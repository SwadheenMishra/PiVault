"""PiVault wire protocol.

Every message (request or response) is one frame:

    [4 bytes header_len][4 bytes payload_len][header JSON][payload bytes]

The header is a small JSON object. The payload is raw bytes (file contents,
directory listings, ...) and is streamed in chunks, never loaded whole into RAM.

Request header:  {"service": "storage", "action": "put", "args": {...}}
Response header: {"ok": true, ...}  or  {"ok": false, "error": "..."}
"""
import asyncio
import json
import os
import struct

PREFIX = struct.Struct(">II")
MAX_HEADER = 64 * 1024
MAX_PAYLOAD = 0xFFFFFFFF  # 4 GiB, limit of the 4-byte length field
CHUNK = 64 * 1024
IO_TIMEOUT = 30  # seconds a single read/write may stall


class ProtocolError(Exception):
    """Client broke the protocol. The connection is dropped."""


class ServiceError(Exception):
    """A normal, user-facing error. Sent back as {"ok": false, "error": ...}."""


class Payload:
    """Lazily-read request payload so large uploads can be streamed to disk."""

    def __init__(self, reader, size):
        self._reader = reader
        self.size = size
        self.remaining = size

    async def read(self, n=CHUNK):
        if self.remaining == 0:
            return b""
        data = await asyncio.wait_for(
            self._reader.read(min(n, self.remaining)), IO_TIMEOUT
        )
        if not data:
            raise asyncio.IncompleteReadError(b"", self.remaining)
        self.remaining -= len(data)
        return data

    async def read_all(self, limit):
        if self.remaining > limit:
            raise ServiceError("payload too large")
        buf = bytearray()
        while self.remaining:
            buf += await self.read()
        return bytes(buf)

    async def discard(self, limit=256 * 1024 * 1024):
        """Throw away unread payload. Returns False if too big (caller must close)."""
        if self.remaining > limit:
            return False
        while self.remaining:
            await self.read()
        return True


async def read_frame_header(reader):
    hlen, plen = PREFIX.unpack(await reader.readexactly(PREFIX.size))
    if not 0 < hlen <= MAX_HEADER:
        raise ProtocolError("bad header length")
    raw = await asyncio.wait_for(reader.readexactly(hlen), IO_TIMEOUT)
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("header is not valid JSON")
    if not isinstance(header, dict):
        raise ProtocolError("header must be a JSON object")
    return header, Payload(reader, plen)


async def send(writer, header, body=b""):
    h = json.dumps(header).encode()
    writer.write(PREFIX.pack(len(h), len(body)) + h + body)
    await asyncio.wait_for(writer.drain(), IO_TIMEOUT)


async def send_file(writer, header, path):
    """Stream a file from disk as the payload of a response."""
    f = await asyncio.to_thread(open, path, "rb")
    try:
        size = os.fstat(f.fileno()).st_size
        if size > MAX_PAYLOAD:
            raise ServiceError("file too large to download")
        h = json.dumps(header).encode()
        writer.write(PREFIX.pack(len(h), size) + h)
        left = size
        while left:
            chunk = await asyncio.to_thread(f.read, min(CHUNK, left))
            if not chunk:
                raise ProtocolError("file shrank while sending")
            writer.write(chunk)
            left -= len(chunk)
            await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
    finally:
        f.close()
