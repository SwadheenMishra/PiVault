"""PiVault server: a small family cloud-storage server over a custom TCP protocol."""
import argparse
import asyncio
import json
import logging
import os
import ssl
from pathlib import Path

from protocol import (
    ProtocolError,
    ServiceError,
    read_frame_header,
    send,
    send_file,
)
from services import App, login_user, need_str, register_user

log = logging.getLogger("pivault")

IDLE_TIMEOUT = 300  # drop connections that sit idle this long
MAX_CONNECTIONS = 20
HANDLERS = {}  # (service, action) -> coroutine


def handler(service, action):
    def deco(fn):
        HANDLERS[(service, action)] = fn
        return fn

    return deco


class Session:
    """State for one connection. Login is tied to the connection."""

    def __init__(self, ip):
        self.ip = ip
        self.user = None


# ----------------------------------------------------------------- auth service


@handler("auth", "register")
async def auth_register(app, sess, args, payload):
    if sess.user:
        raise ServiceError("already logged in")
    sess.user = await register_user(app, sess.ip, args)
    return {"ok": True, "username": sess.user}, b""


@handler("auth", "login")
async def auth_login(app, sess, args, payload):
    if sess.user:
        raise ServiceError("already logged in")
    sess.user = await login_user(app, sess.ip, args)
    return {"ok": True, "username": sess.user}, b""


# -------------------------------------------------------------- storage service


@handler("storage", "list")
async def storage_list(app, sess, args, payload):
    path = app.storage.resolve(sess.user, need_str(args, "path", required=False), allow_root=True)
    if not path.is_dir():
        raise ServiceError("not a folder")
    entries = await asyncio.to_thread(app.storage.list_dir, path)
    return {"ok": True}, json.dumps(entries).encode()


@handler("storage", "mkdir")
async def storage_mkdir(app, sess, args, payload):
    path = app.storage.resolve(sess.user, need_str(args, "path"))
    try:
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    except OSError:
        raise ServiceError("cannot create folder there")
    return {"ok": True}, b""


@handler("storage", "delete")
async def storage_delete(app, sess, args, payload):
    path = app.storage.resolve(sess.user, need_str(args, "path"))
    if not path.exists():
        raise ServiceError("no such file or folder")
    await asyncio.to_thread(app.storage.delete, path)
    log.info("delete user=%s path=%s", sess.user, args["path"])
    return {"ok": True}, b""


@handler("storage", "get")
async def storage_get(app, sess, args, payload):
    path = app.storage.resolve(sess.user, need_str(args, "path"))
    if not path.is_file():
        raise ServiceError("no such file")
    log.info("get user=%s path=%s", sess.user, args["path"])
    return {"ok": True}, path  # a Path body means "stream this file"


@handler("storage", "put")
async def storage_put(app, sess, args, payload):
    path = need_str(args, "path")
    await app.storage.save_stream(sess.user, path, payload.size, payload.read)
    log.info("put user=%s path=%s bytes=%d", sess.user, path, payload.size)
    return {"ok": True, "size": payload.size}, b""


@handler("storage", "rename")
async def storage_rename(app, sess, args, payload):
    await asyncio.to_thread(
        app.storage.rename, sess.user, need_str(args, "path"), need_str(args, "to")
    )
    return {"ok": True}, b""


# ------------------------------------------------------------------- connection


async def handle_connection(app, reader, writer):
    peer = writer.get_extra_info("peername")
    sess = Session(peer[0] if peer else "?")
    if app.active >= MAX_CONNECTIONS:
        writer.close()
        return
    app.active += 1
    log.info("connect ip=%s", sess.ip)
    try:
        while True:
            header, payload = await asyncio.wait_for(
                read_frame_header(reader), IDLE_TIMEOUT
            )
            key = (header.get("service"), header.get("action"))
            args = header.get("args")
            if not isinstance(args, dict):
                args = {}

            keep_open = True
            try:
                fn = HANDLERS.get(key)
                if fn is None:
                    raise ServiceError("unknown service or action")
                if key[0] != "auth" and sess.user is None:
                    raise ServiceError("not logged in")
                if sess.user is None and payload.size:
                    raise ProtocolError("payload before login")
                resp_header, body = await fn(app, sess, args, payload)
            except ServiceError as e:
                keep_open = await payload.discard()
                await send(writer, {"ok": False, "error": str(e)})
                if not keep_open:
                    break
                continue

            if not await payload.discard():
                break
            if isinstance(body, Path):
                await send_file(writer, resp_header, body)
            else:
                await send(writer, resp_header, body)
    except (
        asyncio.IncompleteReadError,
        asyncio.TimeoutError,
        ConnectionError,
        ProtocolError,
        ssl.SSLError,
    ) as e:
        log.info("disconnect ip=%s (%s)", sess.ip, type(e).__name__)
    except Exception:
        log.exception("unexpected error ip=%s", sess.ip)
    finally:
        app.active -= 1
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def amain(args):
    invite = args.invite_code or os.environ.get("PIVAULT_INVITE_CODE", "")
    app = App(args.data_dir, args.quota_mb * 1024 * 1024, invite)

    ctx = None
    if args.cert and args.key:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.cert, args.key)
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "TLS is OFF and listening on %s: passwords travel unencrypted! "
            "Use --cert/--key, or only listen on a WireGuard/localhost address.",
            args.host,
        )
    if not invite:
        log.warning("No invite code set: anyone who can reach the port can register.")

    runner = None
    if args.web_port:
        try:
            from http_api import start_web
        except ImportError as e:
            # show the REAL cause (a missing package, or a mismatched/old file)
            raise SystemExit(
                f"Could not load the web UI: {type(e).__name__}: {e}\n"
                "If this says \"No module named 'aiohttp'\": py -m pip install -r requirements.txt\n"
                "If it names one of our own files, make sure ALL files in server/ are from the same download."
            )
        static = Path(__file__).resolve().parent.parent / "webui"
        runner = await start_web(app, args.web_host or args.host, args.web_port, ctx, static)
        log.info("Web UI on %s://%s:%d", "https" if ctx else "http",
                 args.web_host or args.host, args.web_port)

    server = await asyncio.start_server(
        lambda r, w: handle_connection(app, r, w), args.host, args.port, ssl=ctx
    )
    log.info(
        "PiVault TCP listening on %s:%d (tls=%s, data=%s)",
        args.host, args.port, bool(ctx), Path(args.data_dir).resolve(),
    )
    try:
        async with server:
            await server.serve_forever()
    finally:
        if runner:
            await runner.cleanup()


def main():
    p = argparse.ArgumentParser(description="PiVault family cloud storage server")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (default: localhost only)")
    p.add_argument("--port", type=int, default=9000, help="TCP protocol port")
    p.add_argument("--web-port", type=int, default=8080, help="web UI port (0 = disable)")
    p.add_argument("--web-host", help="address for the web UI (default: same as --host)")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--cert", help="TLS certificate (PEM)")
    p.add_argument("--key", help="TLS private key (PEM)")
    p.add_argument("--invite-code", help="required to register (or env PIVAULT_INVITE_CODE)")
    p.add_argument("--quota-mb", type=int, default=10 * 1024, help="per-user quota in MB")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
