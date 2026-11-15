#!/usr/bin/env python3
"""Tests for config.ini: invite code, storage limits, live reload.

Run from the project root:  python tests/settings_test.py
"""
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import settings as S  # noqa: E402

PASSED = 0
MB = 1024**2
GB = 1024**3


def check(name, cond):
    global PASSED
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        sys.exit(1)
    PASSED += 1


def raises(fn, *a):
    try:
        fn(*a)
    except (ValueError, S.SettingsError):
        return True
    return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bump(path, text):
    """Write and make sure the (mtime,size) signature differs."""
    time.sleep(0.02)
    path.write_text(text, encoding="utf-8")


class Live:
    def __init__(self, data, *extra):
        self.web = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "server.py"), "--data-dir", str(data), "--port", str(free_port()),
             "--web-port", str(self.web), *extra],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
        for _ in range(60):
            try:
                self.api("GET", "/api/config")
                return
            except Exception:
                time.sleep(0.2)
        raise AssertionError("server did not start")

    def api(self, method, path, body=None, cookie=None):
        h = {"X-PiVault": "1", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(f"http://127.0.0.1:{self.web}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), ""

    def register(self, user, code):
        st, _, ck = self.api("POST", "/api/register", {"username": user, "password": "password-123", "invite_code": code})
        return st, ck.split(";")[0]

    def quota(self, cookie):
        return self.api("GET", "/api/me", cookie=cookie)[1]["quota"]

    def stop(self):
        self.proc.terminate()
        self.proc.communicate(timeout=10)


def main():
    print("sizes")
    check("10 GB", S.parse_size("10 GB") == 10 * GB)
    check("units are case-insensitive, space optional", S.parse_size("500mb") == 500 * MB and S.parse_size("2 Tb") == 2 * 1024**4)
    check("decimals", S.parse_size("1.5 GB") == int(1.5 * GB))
    check("a unit is required", raises(S.parse_size, "50"))
    check("junk and unknown units are refused", raises(S.parse_size, "lots") and raises(S.parse_size, "5 PB"))
    check("zero is refused", raises(S.parse_size, "0 GB"))
    check("format_size", S.format_size(50 * GB) == "50 GB" and S.format_size(500 * MB) == "500 MB" and S.format_size(int(1.5 * GB)) == "1.5 GB")

    tmp = Path(tempfile.mkdtemp())
    print("the file")
    p = tmp / "config.ini"
    S.write_settings(p, invite_code="abc-def-ghj", web_host="127.0.0.1")
    t = p.read_text()
    check("created from the commented template", "[storage_per_member]" in t and "# alice = 50 GB" in t)
    st = S.Settings(p)
    st.reload(strict=True)
    check("values read back", st.invite_code == "abc-def-ghj" and st.web_host == "127.0.0.1")
    check("default storage is 10 GB", st.default_quota == 10 * GB and st.overrides == {})
    bump(p, p.read_text().replace("# alice = 50 GB", "Alice = 50 GB").replace("10 GB", "20 GB"))
    st.reload()
    check("hand edits are picked up (names case-insensitive)", st.overrides == {"alice": 50 * GB} and st.default_quota == 20 * GB)
    S.write_settings(p, invite_code="new-code-1")
    t = p.read_text()
    check("writing one key keeps comments and other sections", "Alice = 50 GB" in t and t.startswith("# PiVault settings"))
    bump(p, "﻿" + p.read_text())
    st.reload()
    check("a BOM from Notepad is tolerated", st.invite_code == "new-code-1")
    check("password-style characters like % are kept", (S.write_settings(p, invite_code="50%-off!"), st.reload(), st.invite_code)[2] == "50%-off!")

    print("mistakes")
    bump(p, p.read_text().replace("Alice = 50 GB", "Alice = 50"))
    st.reload()
    check("a typo while running keeps the last good values", st.overrides == {"alice": 50 * GB})
    bump(p, p.read_text().replace("Alice = 50\n", "Alice = 60 GB\n"))
    st.reload()
    check("fixing it is picked up", st.overrides == {"alice": 60 * GB})
    bump(p, p.read_text().replace("Alice = 60 GB", "Alice = 60"))
    try:
        S.Settings(p).reload(strict=True)
        err = ""
    except S.SettingsError as e:
        err = str(e)
    check("at startup the error names the file and the member", "config.ini" in err and "Alice" in err.replace("alice", "Alice") or "alice" in err)

    print("migration from config.json")
    d = tmp / "old"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"invite_code": "old-code-1", "web_host": "0.0.0.0"}))
    m = S.open_settings(d)
    check("old values carried over", m.invite_code == "old-code-1" and m.web_host == "0.0.0.0")
    check("old file renamed", (d / "config.json.migrated").exists() and not (d / "config.json").exists())

    print("a running server")
    data = tmp / "live"
    data.mkdir()
    S.write_settings(data / "config.ini", invite_code="family-one", web_host="127.0.0.1")
    (data / "config.ini").write_text((data / "config.ini").read_text().replace("# alice = 50 GB", "alice = 50 GB").replace("10 GB", "2 GB"))
    srv = Live(data)
    try:
        st1, ck_alice = srv.register("alice", "family-one")
        st2, ck_bob = srv.register("bob", "family-one")
        check("both registered", st1 == 200 and st2 == 200)
        check("alice gets her own limit, bob the default", srv.quota(ck_alice) == 50 * GB and srv.quota(ck_bob) == 2 * GB)
        bump(data / "config.ini", (data / "config.ini").read_text().replace("2 GB", "5 GB"))
        check("changing the default applies without a restart", srv.quota(ck_bob) == 5 * GB)
        bump(data / "config.ini", (data / "config.ini").read_text() + "bob = 250 MB\n")
        check("adding a member line applies without a restart", srv.quota(ck_bob) == 250 * MB)
        check("the old invite code works now", srv.register("carol", "family-one")[0] == 200)
        bump(data / "config.ini", (data / "config.ini").read_text().replace("family-one", "family-two"))
        check("a new invite code applies without a restart", srv.register("dave", "family-one")[0] == 400 and srv.register("dave", "family-two")[0] == 200)
        bump(data / "config.ini", (data / "config.ini").read_text() + "\nbroken = 12\n")
        check("a typo while running does not break the server", srv.quota(ck_alice) == 50 * GB)
    finally:
        srv.stop()

    print("flags")
    data2 = tmp / "flags"
    data2.mkdir()
    S.write_settings(data2 / "config.ini", invite_code="x-code-123", web_host="127.0.0.1")
    (data2 / "config.ini").write_text((data2 / "config.ini").read_text() + "")
    t = (data2 / "config.ini").read_text().replace("# alice = 50 GB", "alice = 50 GB")
    (data2 / "config.ini").write_text(t)
    srv = Live(data2, "--quota-mb", "2")
    try:
        _, ck_a = srv.register("alice", "x-code-123")
        _, ck_b = srv.register("bob", "x-code-123")
        check("--quota-mb beats the file's default", srv.quota(ck_b) == 2 * MB)
        check("...but a member's own line still wins", srv.quota(ck_a) == 50 * GB)
    finally:
        srv.stop()

    print("broken file at startup")
    data3 = tmp / "broken"
    data3.mkdir()
    (data3 / "config.ini").write_text("[pivault]\ndefault_storage = lots\n")
    r = subprocess.run([sys.executable, str(ROOT / "server" / "server.py"), "--data-dir", str(data3)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, cwd=ROOT)
    check("it refuses to start and says what is wrong", r.returncode != 0 and "default_storage" in r.stdout and "lots" in r.stdout)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
