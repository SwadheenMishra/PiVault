"""PiVault mock CLI client. Only for testing the server; the real app comes later.

Commands:
  register                 create an account
  login                    log in
  ls [path]                list a folder
  mkdir <path>             create a folder
  put <local> [remote]     upload a file OR a whole folder (remote ending in / = into that folder)
  get <remote> [local]     download a file OR a whole folder
  rm <path>                delete a file or folder
  help / quit
"""
import argparse
import getpass
import json
import os
import shlex
import socket
import ssl
import struct
import time
from pathlib import Path

PREFIX = struct.Struct(">II")
CHUNK = 64 * 1024
MAX_INLINE = 16 * 1024 * 1024


class VaultError(Exception):
    pass


class VaultClient:
    def __init__(self, host, port, cafile=None, server_name=None):
        raw = socket.create_connection((host, port), timeout=60)
        if cafile:
            ctx = ssl.create_default_context(cafile=cafile)
            self.sock = ctx.wrap_socket(raw, server_hostname=server_name or host)
        else:
            self.sock = raw

    def close(self):
        self.sock.close()

    def _recvn(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VaultError("server closed the connection")
            buf += chunk
        return bytes(buf)

    def request(self, service, action, args=None, body=b"", file=None, save_to=None):
        """Send one request, return (response_header, response_body_bytes)."""
        h = json.dumps({"service": service, "action": action, "args": args or {}}).encode()
        if file and not os.path.isfile(file):
            raise VaultError(f"not a file: {file}")
        size = os.path.getsize(file) if file else len(body)
        try:
            self.sock.sendall(PREFIX.pack(len(h), size) + h)
            if file:
                with open(file, "rb") as f:
                    while chunk := f.read(CHUNK):
                        self.sock.sendall(chunk)
            else:
                self.sock.sendall(body)
        except (BrokenPipeError, ConnectionResetError):
            raise VaultError("server closed the connection while sending")

        hlen, plen = PREFIX.unpack(self._recvn(PREFIX.size))
        header = json.loads(self._recvn(hlen))
        if not header.get("ok"):
            # still consume the payload (normally empty) to stay in sync
            if plen:
                self._recvn(plen)
            raise VaultError(header.get("error", "unknown error"))

        if save_to:
            tmp = Path(str(save_to) + ".part")
            left = plen
            with open(tmp, "wb") as f:
                while left:
                    chunk = self.sock.recv(min(CHUNK, left))
                    if not chunk:
                        raise VaultError("connection lost during download")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, save_to)
            return header, b""
        if plen > MAX_INLINE:
            raise VaultError("response too large")
        return header, self._recvn(plen)

    # convenience wrappers ---------------------------------------------------
    def register(self, username, password, invite_code=""):
        self.request("auth", "register",
                     {"username": username, "password": password, "invite_code": invite_code})

    def login(self, username, password):
        self.request("auth", "login", {"username": username, "password": password})

    def ls(self, path=""):
        return json.loads(self.request("storage", "list", {"path": path})[1])

    def mkdir(self, path):
        self.request("storage", "mkdir", {"path": path})

    def put(self, local, remote):
        self.request("storage", "put", {"path": remote}, file=local)

    def get(self, remote, local):
        self.request("storage", "get", {"path": remote}, save_to=local)

    def rm(self, path):
        self.request("storage", "delete", {"path": path})


def upload_tree(c, local_dir, remote_base):
    """Upload a whole local folder recursively. Returns (files, bytes)."""
    local_dir = Path(local_dir)
    remote_base = remote_base.strip("/")
    c.mkdir(remote_base)
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(local_dir):
        rel = Path(dirpath).relative_to(local_dir).as_posix()
        rel = "" if rel == "." else rel
        for d in dirnames:  # also recreates empty folders
            c.mkdir("/".join(x for x in (remote_base, rel, d) if x))
        for name in filenames:
            remote = "/".join(x for x in (remote_base, rel, name) if x)
            c.put(os.path.join(dirpath, name), remote)
            files += 1
            total += os.path.getsize(os.path.join(dirpath, name))
            print(f"  {remote}")
    return files, total


def safe_name(name):
    """Names come from the server; never let one escape the local target folder."""
    bad = name in ("", ".", "..") or any(ch in name for ch in '/\\:\0')
    if bad:
        raise VaultError(f"refusing unsafe file name from server: {name!r}")
    return name


def download_tree(c, remote_dir, local_dir):
    """Download a remote folder recursively. Returns (files, bytes)."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    files = total = 0
    for e in c.ls(remote_dir):
        name = safe_name(e["name"])
        remote = "/".join(x for x in (remote_dir.strip("/"), name) if x)
        target = local_dir / name
        if e["type"] == "dir":
            n, size = download_tree(c, remote, target)
            files += n
            total += size
        else:
            c.get(remote, str(target))
            files += 1
            total += os.path.getsize(target)
            print(f"  {remote}")
    return files, total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def repl(c):
    user = None
    print(__doc__)
    while True:
        try:
            line = input(f"pivault[{user or 'guest'}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print("parse error:", e)
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd in ("quit", "exit"):
                return
            elif cmd == "help":
                print(__doc__)
            elif cmd == "register":
                u = input("username: ")
                pw = getpass.getpass("password: ")
                if pw != getpass.getpass("repeat password: "):
                    print("passwords do not match")
                    continue
                code = getpass.getpass("invite code (blank if none): ")
                c.register(u, pw, code)
                user = u.strip().lower()
                print("account created, you are logged in")
            elif cmd == "login":
                u = input("username: ")
                c.login(u, getpass.getpass("password: "))
                user = u.strip().lower()
                print("logged in")
            elif cmd == "ls":
                entries = c.ls(rest[0] if rest else "")
                for e in entries:
                    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
                    size = "<dir>" if e["type"] == "dir" else human(e["size"])
                    print(f"{when}  {size:>10}  {e['name']}{'/' if e['type'] == 'dir' else ''}")
                if not entries:
                    print("(empty)")
            elif cmd == "mkdir" and rest:
                c.mkdir(rest[0])
            elif cmd == "put" and rest:
                local = rest[0]
                name = Path(local).resolve().name
                remote = rest[1] if len(rest) > 1 else name
                if remote.endswith("/"):
                    remote += name
                t = time.time()
                if os.path.isdir(local):
                    n, size = upload_tree(c, local, remote)
                    print(f"uploaded folder: {n} files, {human(size)} in {time.time() - t:.1f}s")
                elif os.path.isfile(local):
                    c.put(local, remote)
                    print(f"uploaded {human(os.path.getsize(local))} in {time.time() - t:.1f}s")
                else:
                    print("local path not found:", local)
            elif cmd == "get" and rest:
                remote = rest[0]
                name = Path(remote.replace("\\", "/").rstrip("/")).name or (user or "pivault_root")
                local = rest[1] if len(rest) > 1 else name
                t = time.time()
                try:
                    c.ls(remote)  # works only if it's a folder
                    is_dir = True
                except VaultError:
                    is_dir = False
                if is_dir:
                    n, size = download_tree(c, remote, local)
                    print(f"saved folder {local}: {n} files, {human(size)} in {time.time() - t:.1f}s")
                else:
                    c.get(remote, local)  # raises "no such file" if it doesn't exist
                    print(f"saved {local} ({human(os.path.getsize(local))}) in {time.time() - t:.1f}s")
            elif cmd == "rm" and rest:
                c.rm(rest[0])
            else:
                print("unknown command or missing argument; type 'help'")
        except VaultError as e:
            print("error:", e)
        except (ConnectionError, socket.timeout):
            print("connection lost")
            return
        except OSError as e:
            print("local file error:", e)


def main():
    p = argparse.ArgumentParser(description="PiVault mock client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--cafile", help="server certificate/CA (PEM); enables TLS")
    p.add_argument("--server-name", help="hostname to verify in the certificate")
    a = p.parse_args()
    c = VaultClient(a.host, a.port, a.cafile, a.server_name)
    try:
        repl(c)
    finally:
        c.close()


if __name__ == "__main__":
    main()
