"""File storage. Layout on disk:

    <data_dir>/Storage/<username>/...   user files and folders
    <data_dir>/tmp/                     in-progress uploads (moved into place when complete)
"""
import asyncio
import logging
import os
import secrets
import shutil
from pathlib import Path

from protocol import CHUNK, ServiceError

log = logging.getLogger("pivault")


class Storage:
    def __init__(self, data_dir, quota_bytes):
        base = Path(data_dir).resolve()
        self.root = base / "Storage"
        self.tmp = base / "tmp"
        self.quota = quota_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        if self.tmp.exists():
            shutil.rmtree(self.tmp)  # leftovers from an interrupted upload
        self.tmp.mkdir(parents=True)

    def user_root(self, username):
        p = self.root / username
        p.mkdir(exist_ok=True)
        return p.resolve()

    def resolve(self, username, rel, allow_root=False):
        """Turn a client-supplied relative path into a real path that is
        guaranteed to be inside that user's folder."""
        if not isinstance(rel, str) or "\x00" in rel or len(rel) > 1024:
            raise ServiceError("invalid path")
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." or len(p) > 255 for p in parts):
            raise ServiceError("invalid path")
        if not parts and not allow_root:
            raise ServiceError("path required")
        root = self.user_root(username)
        resolved = root.joinpath(*parts).resolve()
        # Second line of defence: even if something above is wrong (symlinks, odd
        # unicode, ...), refuse anything that ended up outside the user's folder.
        if resolved != root and not resolved.is_relative_to(root):
            raise ServiceError("invalid path")
        return resolved

    async def save_stream(self, username, rel, size, read_chunk):
        """Store an upload of `size` bytes. `read_chunk(n)` is an async callable
        returning up to n bytes (b"" means the stream ended early).
        Used by both the TCP and the HTTP server."""
        target = self.resolve(username, rel)
        if target.is_dir():
            raise ServiceError("a folder with that name already exists")
        used = await asyncio.to_thread(self.used_bytes, username)
        existing = target.stat().st_size if target.is_file() else 0
        if used - existing + size > self.quota:
            raise ServiceError("storage quota exceeded")

        tmp = self.tmp / f"{secrets.token_hex(8)}.part"
        try:
            with open(tmp, "wb") as f:
                received = 0
                while received < size:
                    chunk = await read_chunk(min(CHUNK, size - received))
                    if not chunk:
                        raise ServiceError("upload interrupted")
                    await asyncio.to_thread(f.write, chunk)
                    received += len(chunk)
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, tmp, target)  # atomic
        except OSError:
            log.exception("save failed user=%s", username)
            raise ServiceError("could not store file")
        finally:
            tmp.unlink(missing_ok=True)
        return target

    def rename(self, username, src, dst):
        s = self.resolve(username, src)
        d = self.resolve(username, dst)
        if not s.exists():
            raise ServiceError("no such file or folder")
        if d.exists():
            raise ServiceError("something with that name already exists")
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            os.rename(s, d)
        except OSError:
            raise ServiceError("cannot move it there")

    def used_bytes(self, username):
        total = 0
        for dirpath, _dirs, files in os.walk(self.user_root(username)):
            for name in files:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        return total

    def list_dir(self, path):
        entries = []
        with os.scandir(path) as it:
            for e in it:
                st = e.stat(follow_symlinks=False)
                is_dir = e.is_dir(follow_symlinks=False)
                entries.append(
                    {
                        "name": e.name,
                        "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime),
                    }
                )
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return entries

    @staticmethod
    def delete(path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
