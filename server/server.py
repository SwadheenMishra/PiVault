"""PiVault server: a small family cloud-storage server over a custom TCP protocol."""
import argparse
import asyncio
import errno
import json
import logging
import os
import secrets
import socket
import ssl
import sys
from pathlib import Path

# ------------------------------------------------------------ first-run setup
LOCAL_HOST, LAN_HOST = "127.0.0.1", "0.0.0.0"


def make_invite_code():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters (0/o, 1/l/i)
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def ask_invite_code(ask=input, say=print):
    say("\nStep 1 of 2: invite code")
    say("Family members need this code to create an account. Only share it with people you trust.")
    while True:
        raw = ask("Type a code, or press Enter to generate one: ").strip()
        if not raw:
            code = make_invite_code()
            say(f"  Your invite code is:  {code}   (write it down)")
            return code
        if len(raw) < 6:
            say("  Please use at least 6 characters.")
            continue
        return raw


def ask_access(ask=input, say=print):
    say("\nStep 2 of 2: who can connect?")
    say("  1) Only this computer, plus Tailscale or another tunnel   (safest, recommended)")
    say("  2) Other devices on my home Wi-Fi / LAN                   (plain HTTP, home networks only)")
    while True:
        raw = ask("Choose 1 or 2 [1]: ").strip() or "1"
        if raw == "1":
            return LOCAL_HOST
        if raw == "2":
            say("  If your firewall asks, allow PiVault on private networks.")
            return LAN_HOST
        say("  Please type 1 or 2.")


def resolve_settings(args, ask=input, say=print, interactive=None):
    """Work out the invite code and the web address to listen on.

    Order: command-line flag, then the PIVAULT_INVITE_CODE environment variable,
    then config.ini, then (only in an interactive terminal) the two setup
    questions. Answers are written to config.ini so they are only asked once.
    Returns (explicit_invite, web_host, settings)."""
    if interactive is None:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    settings = open_settings(args.data_dir)
    saved_invite = "" if args.setup else settings.invite_code
    saved_host = "" if args.setup else settings.web_host
    explicit = args.invite_code or os.environ.get("PIVAULT_INVITE_CODE") or ""
    invite = explicit or saved_invite
    web_host = args.web_host or saved_host or None

    asked = {}
    if interactive and (not invite or not web_host):
        say("\nWelcome to PiVault! Two quick questions, then it starts.")
        if not invite:
            asked["invite_code"] = invite = ask_invite_code(ask, say)
        if not web_host:
            asked["web_host"] = web_host = ask_access(ask, say)
    if asked:
        path = write_settings(settings.path, **asked)
        settings.reload(strict=True)
        say(f"\nSaved to {path}. Edit that file any time to change the invite code or storage limits.")
    elif saved_invite or saved_host:
        say(f"Using your settings file ({settings.path}).")
    if not args.web_host and web_host:
        args.web_host = web_host
    return explicit, web_host, settings


def lan_ip():
    """This computer's address on the local network (no traffic is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def pause_if_double_clicked():
    """A double-clicked .exe closes its window the instant it stops. Keep it open so
    people can read the error."""
    if FROZEN and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


from protocol import (
    ProtocolError,
    ServiceError,
    read_frame_header,
    send,
    send_file,
)
from settings import SettingsError, open_settings, write_settings
from services import App, login_user, need_str, register_user

log = logging.getLogger("pivault")

# When packaged with PyInstaller (--onefile), bundled files are unpacked to a temp
# folder (sys._MEIPASS) and __file__ no longer points at the project. These two
# helpers make the server work both from source and as a packaged executable.
FROZEN = getattr(sys, "frozen", False)


def bundled_path(name):
    """Folder of bundled read-only files such as the web UI."""
    base = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent.parent
    return base / name


def default_data_dir():
    """Next to the .exe when packaged (so double-clicking works from anywhere),
    otherwise ./data as before."""
    return (Path(sys.executable).resolve().parent / "data") if FROZEN else Path("./data")

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


async def amain(args, settings):
    explicit_quota = args.quota_mb * 1024 * 1024 if args.quota_mb else None
    app = App(args.data_dir, settings, args.invite_code, explicit_quota)
    invite = app.invite_code

    ctx = None
    local = ("127.0.0.1", "localhost", "::1")
    exposed = sorted({h for h in (args.host, args.web_host or args.host) if h not in local})
    if args.cert and args.key:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.cert, args.key)
    elif exposed:
        log.warning(
            "TLS is OFF and listening on %s: passwords travel unencrypted! "
            "Use --cert/--key, or only listen on a localhost/Tailscale/WireGuard address.",
            ", ".join(exposed),
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
        static = bundled_path("webui")
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
    if args.web_port:
        scheme = "https" if ctx else "http"
        shown = args.web_host or args.host
        lines = [f"  On this computer:    {scheme}://localhost:{args.web_port}"]
        if shown in ("0.0.0.0", "::"):
            ip = lan_ip()
            if ip:
                lines.append(f"  From other devices:  {scheme}://{ip}:{args.web_port}   (same Wi-Fi / LAN)")
        elif shown not in local:
            lines.append(f"  On {shown}:  {scheme}://{shown}:{args.web_port}")
        lines.append(f"  Settings file:       {settings.path}")
        lines.append("    (edit it to change the invite code or storage; no restart needed)")
        lines.append(f"  Storage per member:  {app.describe_quotas()}")
        print("\nPiVault is running.\n" + "\n".join(lines) + "\n  To stop it, press Ctrl+C or close this window.\n", flush=True)
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
    p.add_argument("--data-dir", default=str(default_data_dir()), help="where accounts and files are stored")
    p.add_argument("--cert", help="TLS certificate (PEM)")
    p.add_argument("--key", help="TLS private key (PEM)")
    p.add_argument("--invite-code", help="required to register (default: asked on first run and saved in config.ini; or env PIVAULT_INVITE_CODE)")
    p.add_argument("--setup", action="store_true", help="ask the setup questions again (invite code, who can connect)")
    p.add_argument("--quota-mb", type=int, help="storage per member in MB (overrides default_storage in config.ini; a member's own line still wins)")
    args = p.parse_args()
    try:
        args.invite_code, _, settings = resolve_settings(args)
    except SettingsError as e:
        print(f"\n{e}\nFix that line in the settings file and start PiVault again.")
        pause_if_double_clicked()
        sys.exit(1)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        asyncio.run(amain(args, settings))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 10048):
            print(f"\nCould not start: that port is already in use. Is PiVault already running "
                  f"in another window? Otherwise pick other ports with --web-port and --port.")
        else:
            print(f"\nCould not start PiVault: {e.strerror or e}")
        pause_if_double_clicked()
        sys.exit(1)
    except SystemExit as e:
        if e.code:
            print(f"\n{e.code}", file=sys.stderr)
            pause_if_double_clicked()
        raise SystemExit(1 if e.code else 0)


if __name__ == "__main__":
    main()
