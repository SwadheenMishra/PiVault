"""End-to-end test of the web API. Starts a real server (TCP + HTTP).

Run from the project root:  python tests/web_test.py
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
from client import VaultClient  # noqa: E402

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


class Web:
    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.op = build_opener(HTTPCookieProcessor(self.jar))

    def req(self, method, path, data=None, json_body=None, headers=None, csrf=True):
        h = dict(headers or {})
        if csrf:
            h["X-PiVault"] = "1"
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        try:
            r = self.op.open(Request(self.base + path, data=data, method=method, headers=h))
        except HTTPError as e:
            r = e
        return r.code, r.headers, r.read()

    def j(self, method, path, **kw):
        code, hdr, body = self.req(method, path, **kw)
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None

    def token(self):
        return next(c.value for c in self.jar if c.name == "pv_session")


def main():
    tmp = Path(tempfile.mkdtemp())
    tcp, http = free_port(), free_port()
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "server.py"), "--port", str(tcp),
         "--web-port", str(http), "--data-dir", str(tmp / "data"),
         "--invite-code", "family123", "--quota-mb", "5"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{http}"
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", http), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        a = Web(base)

        print("static + headers")
        code, hdr, body = a.req("GET", "/")
        check("GET / serves the app", code == 200 and b"PiVault" in body)
        check("CSP forbids inline script", "script-src 'self'" in hdr["Content-Security-Policy"])
        check("nosniff + frame deny", hdr["X-Content-Type-Options"] == "nosniff" and hdr["X-Frame-Options"] == "DENY")
        check("app.js / style.css served", a.req("GET", "/static/app.js")[0] == 200 and a.req("GET", "/static/style.css")[0] == 200)
        check("no directory traversal via /static", a.req("GET", "/static/../server/auth.py")[0] in (403, 404))
        check("config says invite required, nobody signed in", a.j("GET", "/api/config")[1] == {"invite_required": True, "user": None})

        print("auth + csrf")
        check("unauthenticated -> 401", a.j("GET", "/api/list")[0] == 401)
        check("POST without X-PiVault header -> 403",
              a.j("POST", "/api/register", csrf=False, json_body={"username": "alice", "password": "password1", "invite_code": "family123"})[0] == 403)
        check("wrong invite code -> 400",
              a.j("POST", "/api/register", json_body={"username": "alice", "password": "password1", "invite_code": "x"})[0] == 400)
        check("bad username -> 400",
              a.j("POST", "/api/register", json_body={"username": "../x", "password": "password1", "invite_code": "family123"})[0] == 400)
        code, hdr, _ = a.req("POST", "/api/register", json_body=None,
                             data=json.dumps({"username": "alice", "password": "correct horse", "invite_code": "family123"}).encode(),
                             headers={"Content-Type": "application/json"})
        cookie = " ".join(hdr.get_all("Set-Cookie") or [])
        check("register ok", code == 200)
        check("cookie is HttpOnly + SameSite=Strict", "HttpOnly" in cookie and "SameSite=Strict" in cookie)
        me = a.j("GET", "/api/me")[1]
        check("/api/me", me["username"] == "alice" and me["quota"] == 5 * 1024 * 1024)
        check("config now reports the signed-in user", a.j("GET", "/api/config")[1]["user"] == "alice")

        print("files")
        check("mkdir", a.j("POST", "/api/mkdir", json_body={"path": "photos/2026/empty"})[0] == 200)
        blob = os.urandom(3 * 1024 * 1024 + 7)
        code, _ = a.j("PUT", "/api/file?path=" + quote("photos/2026/a b.bin"), data=blob)
        check("upload 3MB", code == 200)
        names = [e["name"] for e in a.j("GET", "/api/list?path=photos/2026")[1]["entries"]]
        check("list shows upload + folder", sorted(names) == ["a b.bin", "empty"])
        code, hdr, body = a.req("GET", "/api/file?path=" + quote("photos/2026/a b.bin"))
        check("download is byte-identical", code == 200 and body == blob)
        check("unknown types are attachments", "attachment" in hdr["Content-Disposition"] and hdr["Content-Type"] == "application/octet-stream")

        print("hostile uploads can't run script")
        a.j("PUT", "/api/file?path=evil.html", data=b"<script>alert(1)</script>")
        a.j("PUT", "/api/file?path=evil.svg", data=b"<svg onload=alert(1)/>")
        for f in ("evil.html", "evil.svg"):
            code, hdr, _ = a.req("GET", f"/api/file?path={f}")
            check(f"{f}: forced download, sandboxed, nosniff",
                  "attachment" in hdr["Content-Disposition"]
                  and hdr["Content-Type"] == "application/octet-stream"
                  and "sandbox" in hdr["Content-Security-Policy"]
                  and hdr["X-Content-Type-Options"] == "nosniff")
        a.j("PUT", "/api/file?path=pic.png", data=b"\x89PNG" + b"0" * 100)
        code, hdr, _ = a.req("GET", "/api/file?path=pic.png")
        check("png is shown inline", hdr["Content-Type"] == "image/png" and "inline" in hdr["Content-Disposition"])
        code, hdr, body = a.req("GET", "/api/file?path=pic.png", headers={"Range": "bytes=0-9"})
        check("Range requests work (video seeking)", code == 206 and len(body) == 10)
        a.j("DELETE" if False else "POST", "/api/delete", json_body={"path": "evil.html"})
        a.j("POST", "/api/delete", json_body={"path": "evil.svg"})
        a.j("POST", "/api/delete", json_body={"path": "pic.png"})

        print("quota (5 MB)")
        code, body = a.j("PUT", "/api/file?path=second.bin", data=blob)
        check("second 3MB upload rejected with a readable error", code == 400 and "quota" in body["error"])
        check("connection/session still fine afterwards", a.j("GET", "/api/me")[0] == 200)

        print("traversal + isolation")
        b = Web(base)
        code, _ = b.j("POST", "/api/register", json_body={"username": "bob", "password": "bobs password", "invite_code": "family123"})
        check("bob registers", code == 200)
        check("bob sees nothing of alice's", b.j("GET", "/api/list")[1]["entries"] == [])
        check("bob can't read alice's file", b.req("GET", "/api/file?path=" + quote("photos/2026/a b.bin"))[0] == 404)
        for evil in ["../alice/photos/2026/a b.bin", "a/../../alice/x", "..", "..\\alice\\x"]:
            check(f"traversal blocked (GET) {evil!r}", b.req("GET", "/api/file?path=" + quote(evil))[0] in (400, 404))
            check(f"traversal blocked (PUT) {evil!r}", b.j("PUT", "/api/file?path=" + quote(evil), data=b"x")[0] == 400)
            check(f"traversal blocked (list) {evil!r}", b.j("GET", "/api/list?path=" + quote(evil))[0] in (400, 404))
        check("bob can't delete alice's folder", b.j("POST", "/api/delete", json_body={"path": "../alice/photos"})[0] == 400)
        check("alice's file untouched", (tmp / "data/Storage/alice/photos/2026/a b.bin").is_file())

        print("rename + zip")
        check("rename file", a.j("POST", "/api/rename", json_body={"path": "photos/2026/a b.bin", "to": "photos/2026/renamed.bin"})[0] == 200)
        a.j("PUT", "/api/file?path=photos/2026/two.txt", data=b"hello")
        code, body = a.j("POST", "/api/rename", json_body={"path": "photos/2026/two.txt", "to": "photos/2026/renamed.bin"})
        check("rename onto existing refused", code == 400)
        check("can't move a folder into itself", a.j("POST", "/api/rename", json_body={"path": "photos", "to": "photos/2026/inside"})[0] == 400)
        code, hdr, body = a.req("GET", "/api/zip?path=photos")
        check("zip download", code == 200 and hdr["Content-Type"] == "application/zip" and "photos.zip" in hdr["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(body))
        check("zip is valid", zf.testzip() is None)
        check("zip has correct contents", zf.read("2026/renamed.bin") == blob and zf.read("2026/two.txt") == b"hello")
        check("zip keeps empty folders", "2026/empty/" in zf.namelist())
        check("zip of non-folder -> 404", a.req("GET", "/api/zip?path=" + quote("photos/2026/two.txt"))[0] == 404)

        print("web <-> TCP share the same data")
        c = VaultClient("127.0.0.1", tcp)
        c.login("alice", "correct horse")
        check("file uploaded via web is visible over TCP", "photos" in [e["name"] for e in c.ls()])
        c.close()

        print("sessions")
        token = a.token()
        check("delete works", a.j("POST", "/api/delete", json_body={"path": "photos"})[0] == 200)
        check("logout", a.j("POST", "/api/logout", json_body={})[0] == 200)
        check("after logout -> 401", a.j("GET", "/api/me")[0] == 401)
        replay = Web(base)
        code, _ = replay.j("GET", "/api/me", headers={"Cookie": f"pv_session={token}"})
        check("old token is dead server-side", code == 401)
        check("garbage token rejected", replay.j("GET", "/api/me", headers={"Cookie": "pv_session=abc"})[0] == 401)
        check("tokens are hashed in the database",
              token.encode() not in (tmp / "data/users.db").read_bytes())

        print("brute force")
        x = Web(base)
        codes = [x.j("POST", "/api/login", json_body={"username": "alice", "password": f"wrong-pass-{i}"})[0] for i in range(6)]
        check("lockout after 5 failures (HTTP 429)", codes[:5] == [400] * 5 and codes[5] == 429)
        check("correct password blocked while locked", x.j("POST", "/api/login", json_body={"username": "alice", "password": "correct horse"})[0] == 429)

        print("behind a tunnel (ngrok-style X-Forwarded-For)")
        y = Web(base)
        def try_login(ip, user, pw):
            return y.j("POST", "/api/login", json_body={"username": user, "password": pw},
                       headers={"X-Forwarded-For": ip})[0]
        for i in range(5):
            try_login("203.0.113.7", "nobody1", f"bad-pass-{i}")
        check("attacker's IP gets locked out", try_login("203.0.113.7", "nobody2", "bad-pass-x") == 429)
        check("a different real client is NOT locked out", try_login("198.51.100.9", "nobody3", "bad-pass-y") == 400)
        for i in range(5):
            try_login("evil, 198.51.100.55", "nobody4", f"bad-pass-{i}")
        check("forged leading X-Forwarded-For entries are ignored (last entry counts)",
              try_login("something-else, 198.51.100.55", "nobody5", "bad-pass-z") == 429)

        print(f"\nall {PASSED} checks passed")
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()
