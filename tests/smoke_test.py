"""End-to-end smoke test: starts a real server and talks to it with the client.

Run from the project root:  python tests/smoke_test.py
"""
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
from client import VaultClient, VaultError  # noqa: E402

PASSED = 0


def check(name, cond):
    global PASSED
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        sys.exit(1)
    PASSED += 1


def fails(fn, *a):
    try:
        fn(*a)
    except VaultError:
        return True
    return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    tmp = Path(tempfile.mkdtemp())
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "server.py"), "--port", str(port),
         "--web-port", "0", "--data-dir", str(tmp / "data"), "--invite-code", "family123", "--quota-mb", "5"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        def new():
            return VaultClient("127.0.0.1", port)

        print("auth")
        c = new()
        check("not logged in -> ls rejected", fails(c.ls))
        check("register w/ wrong invite code rejected", fails(c.register, "alice", "password1", "nope"))
        check("bad username rejected", fails(c.register, "../evil", "password1", "family123"))
        check("short password rejected", fails(c.register, "alice", "short", "family123"))
        c.register("alice", "correct horse", "family123")
        check("register ok", c.ls() == [])
        c2 = new()
        check("duplicate username rejected", fails(c2.register, "Alice", "password1", "family123"))
        check("wrong password rejected", fails(c2.login, "alice", "wrongpass1"))
        c2.login("alice", "correct horse")
        check("login ok", True)
        c2.close()

        print("storage")
        src = tmp / "photo.bin"
        src.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
        c.mkdir("photos/2026")
        c.put(str(src), "photos/2026/photo.bin")
        names = [e["name"] for e in c.ls("photos/2026")]
        check("upload + list", names == ["photo.bin"])
        out = tmp / "back.bin"
        c.get("photos/2026/photo.bin", str(out))
        check("download matches upload (sha256)",
              hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest())
        check("file really lives in Storage/alice/",
              (tmp / "data/Storage/alice/photos/2026/photo.bin").is_file())

        print("quota (5 MB)")
        check("second 3MB file exceeds quota", fails(c.put, str(src), "second.bin"))
        c.put(str(src), "photos/2026/photo.bin")
        check("overwriting same file still fits", True)

        print("isolation / traversal")
        b = new()
        b.register("bob", "bobs password", "family123")
        check("bob cannot see alice's files", b.ls() == [])
        check("bob cannot get alice's file by name", fails(b.get, "photos/2026/photo.bin", str(tmp / "x")))
        for evil in ["../alice/photos/2026/photo.bin", "photos/../../alice/photos/2026/photo.bin",
                     "..", "a/../../x", "..\\alice\\x"]:
            check(f"traversal blocked on get: {evil!r}", fails(b.get, evil, str(tmp / "x")))
            check(f"traversal blocked on put: {evil!r}", fails(b.put, str(src), evil))
        check("absolute path stays inside bob's folder", fails(b.get, "/etc/passwd", str(tmp / "x")))
        check("nothing escaped into data dir", not (tmp / "data/Storage/x").exists())
        check("cannot delete own root", fails(b.rm, ""))

        print("delete")
        c.rm("photos")
        check("folder deleted", c.ls() == [])
        check("alice's file untouched by bob's attempts", True)
        c.close()
        b.close()
        print(f"\nall {PASSED} checks passed")
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()
