#!/usr/bin/env python3
"""Tests for the first-run questions (invite code, who can connect).

The questions only appear in an interactive terminal, so the end-to-end part runs the
real server inside a pseudo-terminal and types the answers. Linux/macOS only.

Run from the project root:  python tests/setup_test.py
"""
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import pty
except ImportError:
    print("skipped: this test needs the pty module (Linux or macOS)")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import server as srv  # noqa: E402
import settings as cfgmod  # noqa: E402


def read_cfg(path):
    c = cfgmod.Settings(path)
    c.reload(strict=True)
    return {"invite_code": c.invite_code, "web_host": c.web_host}

PASSED = 0


def check(name, cond):
    global PASSED
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        sys.exit(1)
    PASSED += 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Session:
    """The real server running behind a pseudo-terminal, so isatty() is True."""

    def __init__(self, args):
        self.web = free_port()
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "server.py"), "--port", str(free_port()),
             "--web-port", str(self.web), *args],
            stdin=slave, stdout=slave, stderr=slave, cwd=ROOT, close_fds=True)
        os.close(slave)
        self.buf = ""

    def expect(self, pattern, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            m = re.search(pattern, self.buf)
            if m:
                return m
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    data = os.read(self.master, 4096)
                except OSError:
                    break
                if not data:
                    break
                self.buf += data.decode(errors="replace")
        raise AssertionError(f"timed out waiting for {pattern!r}. Output so far:\n{self.buf[-900:]}")

    def send(self, text):
        os.write(self.master, text.encode())

    def api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.web}{path}", data=data, method=method,
                                     headers={"X-PiVault": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def register(self, user, code):
        return self.api("POST", "/api/register", {"username": user, "password": "password-123", "invite_code": code})[0]

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        os.close(self.master)


def main():
    print("the prompts themselves")
    said = []
    scripted = iter(["abc", "", ])
    code = srv.ask_invite_code(ask=lambda _: next(scripted), say=said.append)
    check("a too-short code is refused", any("at least 6" in m for m in said))
    check("pressing Enter generates a code like abcd-efgh-jk23", re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", code) is not None)
    check("generated codes avoid look-alike characters", not set("0o1li") & set(code.replace("-", "")))
    check("typed codes are used as typed", srv.ask_invite_code(ask=lambda _: "  my-family-code ", say=lambda *_: None) == "my-family-code")
    scripted = iter(["x", "", ])
    check("access: junk is refused, Enter means safest (127.0.0.1)", srv.ask_access(ask=lambda _: next(scripted), say=lambda *_: None) == "127.0.0.1")
    check("access: 2 means the LAN (0.0.0.0)", srv.ask_access(ask=lambda _: "2", say=lambda *_: None) == "0.0.0.0")

    tmp = Path(tempfile.mkdtemp())

    print("first run in a terminal")
    d = str(tmp / "data1")
    s1 = Session(["--data-dir", d])
    s1.expect("Step 1 of 2")
    s1.send("ab\n")
    s1.expect("at least 6 characters")
    s1.send("\n")
    generated = s1.expect(r"Your invite code is:\s+([a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})").group(1)
    s1.expect("Step 2 of 2")
    s1.send("7\n")
    s1.expect("Please type 1 or 2")
    s1.send("2\n")
    s1.expect("PiVault is running")
    check("it asked both questions and started", True)
    if srv.lan_ip():
        s1.expect("From other devices")
        check("choosing 2 prints the address to open from other devices", True)
    cfg_path = Path(d) / "config.ini"
    cfg = read_cfg(cfg_path)
    check("answers were saved in config.ini", cfg == {"invite_code": generated, "web_host": "0.0.0.0"})
    if os.name == "posix":
        check("config file is owner-only (chmod 600)", stat.S_IMODE(cfg_path.stat().st_mode) == 0o600)
    check("the web UI now requires an invite code", s1.api("GET", "/api/config")[1]["invite_required"] is True)
    check("a wrong code is rejected", s1.register("wrongcode", "nope-nope") == 400)
    check("the generated code works", s1.register("alice", generated) == 200)
    s1.expect("Storage per member:")
    check("the start-up summary names the settings file and storage", "Settings file:" in s1.buf and "10 GB" in s1.buf)
    s1.stop()
    cfgmod.write_settings(cfg_path)  # no-op rewrite keeps everything
    text = cfg_path.read_text().replace("# alice = 50 GB", "alice = 50 GB")
    cfg_path.write_text(text)

    print("second run: nothing is asked")
    s2 = Session(["--data-dir", d])
    s2.expect("PiVault is running")
    check("it reuses the saved settings without asking", "Step 1" not in s2.buf and "Using your settings file" in s2.buf)
    check("the saved invite code is enforced", s2.register("bob", generated) == 200 and s2.register("eve", "wrong-code-x") == 400)
    s2.expect("Storage per member:")
    check("the member's own limit shows in the summary", "alice 50 GB" in s2.buf)
    s2.stop()

    print("--setup asks again")
    s3 = Session(["--data-dir", d, "--setup"])
    s3.expect("Step 1 of 2")
    s3.send("brand-new-code\n")
    s3.expect("Step 2 of 2")
    s3.send("1\n")
    s3.expect("PiVault is running")
    check("choosing 1 (this computer / Tailscale) hides the LAN address", "From other devices" not in s3.buf)
    check("the new answers replaced the old ones",
          read_cfg(cfg_path) == {"invite_code": "brand-new-code", "web_host": "127.0.0.1"})
    check("the old code stopped working, the new one works", s3.register("carol", generated) == 400 and s3.register("carol", "brand-new-code") == 200)
    check("--setup keeps the member's own storage limit", "alice = 50 GB" in cfg_path.read_text())
    s3.stop()

    print("when it must not ask")
    d2 = str(tmp / "data2")
    s4 = Session(["--data-dir", d2, "--invite-code", "flag-code-123", "--web-host", "127.0.0.1"])
    s4.expect("PiVault is running")
    check("flags answer both questions, so nothing is asked", "Step 1" not in s4.buf and "Step 2" not in s4.buf)
    check("flag-provided values are not written to disk", not (Path(d2) / "config.ini").exists())
    s4.stop()

    d3 = str(tmp / "data3")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "server.py"), "--data-dir", d3, "--web-port", str(w := free_port()), "--port", str(free_port())],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    time.sleep(2.5)
    req = urllib.request.Request(f"http://127.0.0.1:{w}/api/config")
    with urllib.request.urlopen(req, timeout=5) as r:
        started = json.loads(r.read())
    proc.terminate()
    out = proc.communicate(timeout=10)[0].decode(errors="replace")
    check("without a terminal (service/Docker) it never waits for input", started["invite_required"] is False and "Step 1" not in out)
    check("...and warns that no invite code is set", "No invite code set" in out)

    print(f"\nall {PASSED} checks passed")


if __name__ == "__main__":
    main()
