"""HTTP API + static web UI. Shares App/Storage/UserDB with the TCP server."""
import asyncio
import logging
import os
import zipfile
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from protocol import CHUNK, IO_TIMEOUT, ServiceError
from services import RateLimitError, login_user, need_str, register_user

log = logging.getLogger("pivault.web")

COOKIE = "pv_session"
MAX_DRAIN = 256 * 1024 * 1024

# Only these are ever shown inline. Everything else (html, svg, pdf, ...) is forced
# to download, so an uploaded file can never run script in the site's origin.
INLINE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".wav": "audio/wav",
}

APP_CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
FILE_CSP = "default-src 'none'; sandbox"


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def vault(request):
    return request.app["vault"]


# ------------------------------------------------------------------ middleware


@web.middleware
async def guard(request, handler):
    # CSRF defence in depth (cookie is also SameSite=Strict): browsers cannot add
    # a custom header on a cross-site request without a CORS preflight, which we refuse.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-PiVault") != "1":
        return web.json_response({"error": "missing X-PiVault header"}, status=403)
    try:
        return await handler(request)
    except ApiError as e:
        return web.json_response({"error": e.message}, status=e.status)
    except RateLimitError as e:
        return web.json_response({"error": str(e)}, status=429)
    except ServiceError as e:
        return web.json_response({"error": str(e)}, status=400)
    except web.HTTPException:
        raise
    except (asyncio.TimeoutError, ConnectionResetError):
        return web.json_response({"error": "connection timed out"}, status=408)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.path)
        return web.json_response({"error": "internal error"}, status=500)


async def add_headers(request, response):
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy", APP_CSP)
    h.setdefault("Cache-Control", "no-store" if request.path.startswith("/api/") else "no-cache")


# --------------------------------------------------------------------- helpers


def client_ip(request):
    """Real client address. Behind a local tunnel/proxy (ngrok, Caddy, cloudflared)
    every connection comes from 127.0.0.1, so use the proxy's X-Forwarded-For --
    but only when the connection really is from this machine, and only the LAST
    entry (the one our own proxy appended; earlier entries can be forged by the client)."""
    peer = request.remote or "?"
    if peer in ("127.0.0.1", "::1"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip()[:64] or peer
    return peer


async def json_body(request):
    try:
        data = await request.json()
    except Exception:
        raise ApiError(400, "invalid JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, "JSON body must be an object")
    return data


async def require_user(request):
    token = request.cookies.get(COOKIE)
    if token:
        user = await asyncio.to_thread(vault(request).users.session_user, token)
        if user:
            return user
    raise ApiError(401, "not signed in")


def set_session_cookie(request, resp, token):
    secure = request.secure or request.headers.get("X-Forwarded-Proto") == "https"
    resp.set_cookie(COOKIE, token, max_age=14 * 24 * 3600, path="/",
                    httponly=True, samesite="Strict", secure=secure)


async def start_session(request, username):
    token = await asyncio.to_thread(vault(request).users.create_session, username)
    resp = web.json_response({"username": username})
    set_session_cookie(request, resp, token)
    return resp


def content_disposition(kind, name):
    safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"{kind}; filename=\"{safe}\"; filename*=UTF-8''{quote(name)}"


async def drain(request):
    """Read and discard the rest of an upload so the client gets our error message."""
    left = request.content_length or 0
    if left > MAX_DRAIN:
        return
    while left > 0:
        chunk = await request.content.read(CHUNK)
        if not chunk:
            break
        left -= len(chunk)


# -------------------------------------------------------------------- handlers


async def api_config(request):
    """Public bootstrap info. Also says who is signed in (or null), so the page
    never has to make a request that is expected to fail."""
    user = None
    token = request.cookies.get(COOKIE)
    if token:
        user = await asyncio.to_thread(vault(request).users.session_user, token)
    return web.json_response({"invite_required": bool(vault(request).invite_code), "user": user})


async def api_register(request):
    data = await json_body(request)
    username = await register_user(vault(request), client_ip(request), data)
    return await start_session(request, username)


async def api_login(request):
    data = await json_body(request)
    username = await login_user(vault(request), client_ip(request), data)
    return await start_session(request, username)


async def api_logout(request):
    token = request.cookies.get(COOKIE)
    if token:
        await asyncio.to_thread(vault(request).users.delete_session, token)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_me(request):
    user = await require_user(request)
    app = vault(request)
    used = await asyncio.to_thread(app.storage.used_bytes, user)
    return web.json_response({"username": user, "used": used, "quota": app.storage.quota})


async def api_list(request):
    user = await require_user(request)
    st = vault(request).storage
    path = st.resolve(user, request.query.get("path", ""), allow_root=True)
    if not path.is_dir():
        raise ApiError(404, "no such folder")
    entries = await asyncio.to_thread(st.list_dir, path)
    return web.json_response({"entries": entries})


async def api_mkdir(request):
    user = await require_user(request)
    data = await json_body(request)
    path = vault(request).storage.resolve(user, need_str(data, "path"))
    try:
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    except OSError:
        raise ApiError(400, "cannot create a folder there")
    return web.json_response({"ok": True})


async def api_delete(request):
    user = await require_user(request)
    data = await json_body(request)
    st = vault(request).storage
    path = st.resolve(user, need_str(data, "path"))
    if not path.exists():
        raise ApiError(404, "no such file or folder")
    await asyncio.to_thread(st.delete, path)
    log.info("delete user=%s path=%s", user, data["path"])
    return web.json_response({"ok": True})


async def api_rename(request):
    user = await require_user(request)
    data = await json_body(request)
    await asyncio.to_thread(
        vault(request).storage.rename, user, need_str(data, "path"), need_str(data, "to")
    )
    return web.json_response({"ok": True})


async def api_put_file(request):
    user = await require_user(request)
    size = request.content_length
    if size is None:
        raise ApiError(411, "Content-Length required")
    path = request.query.get("path", "")
    try:
        await vault(request).storage.save_stream(
            user, path, size,
            lambda n: asyncio.wait_for(request.content.read(n), IO_TIMEOUT),
        )
    except ServiceError:
        await drain(request)
        raise
    log.info("put user=%s path=%s bytes=%d", user, path, size)
    return web.json_response({"ok": True, "size": size})


async def api_get_file(request):
    user = await require_user(request)
    path = vault(request).storage.resolve(user, request.query.get("path", ""))
    if not path.is_file():
        raise ApiError(404, "no such file")
    ctype = INLINE_TYPES.get(path.suffix.lower())
    force_download = request.query.get("download") == "1"
    if ctype and not force_download:
        headers = {"Content-Type": ctype, "Content-Disposition": content_disposition("inline", path.name)}
    else:
        headers = {"Content-Type": "application/octet-stream",
                   "Content-Disposition": content_disposition("attachment", path.name)}
    headers["Content-Security-Policy"] = FILE_CSP
    headers["Cache-Control"] = "private, no-cache"
    return web.FileResponse(path, headers=headers)  # supports Range (video seeking)


async def api_zip(request):
    """Stream a folder as a zip without building it in RAM or on disk."""
    user = await require_user(request)
    st = vault(request).storage
    folder = st.resolve(user, request.query.get("path", ""), allow_root=True)
    if not folder.is_dir():
        raise ApiError(404, "no such folder")
    name = (folder.name if folder != st.user_root(user) else user) + ".zip"
    resp = web.StreamResponse(headers={
        "Content-Type": "application/zip",
        "Content-Disposition": content_disposition("attachment", name),
    })
    await resp.prepare(request)
    loop = asyncio.get_running_loop()

    class Sink:  # unseekable file-like; zipfile switches to streaming mode
        def write(self, data):
            if data:
                asyncio.run_coroutine_threadsafe(resp.write(bytes(data)), loop).result(IO_TIMEOUT)
            return len(data)

        def flush(self):
            pass

    def build():
        # ZIP_STORED: photos/videos are already compressed, so don't waste Pi CPU.
        with zipfile.ZipFile(Sink(), "w", zipfile.ZIP_STORED, allowZip64=True,
                             strict_timestamps=False) as zf:
            for dirpath, dirs, files in os.walk(folder):
                rel = os.path.relpath(dirpath, folder)
                prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
                if prefix and not dirs and not files:
                    zf.writestr(prefix, "")  # keep empty folders
                for f in files:
                    full = os.path.join(dirpath, f)
                    if not os.path.islink(full):
                        zf.write(full, prefix + f)

    log.info("zip user=%s path=%s", user, request.query.get("path", ""))
    try:
        await asyncio.to_thread(build)
        await resp.write_eof()
    except Exception as e:  # client went away mid-download, etc.
        log.info("zip aborted user=%s (%s)", user, type(e).__name__)
    return resp


async def index(request):
    return web.FileResponse(request.app["static_dir"] / "index.html")


def build_app(vault_app, static_dir):
    app = web.Application(middlewares=[guard], client_max_size=1024 * 1024)
    app["vault"] = vault_app
    app["static_dir"] = Path(static_dir)
    app.on_response_prepare.append(add_headers)
    app.add_routes([
        web.get("/", index),
        web.get("/api/config", api_config),
        web.post("/api/register", api_register),
        web.post("/api/login", api_login),
        web.post("/api/logout", api_logout),
        web.get("/api/me", api_me),
        web.get("/api/list", api_list),
        web.post("/api/mkdir", api_mkdir),
        web.post("/api/delete", api_delete),
        web.post("/api/rename", api_rename),
        web.put("/api/file", api_put_file),
        web.get("/api/file", api_get_file),
        web.get("/api/zip", api_zip),
    ])
    app.router.add_static("/static/", app["static_dir"], follow_symlinks=False)
    return app


async def start_web(vault_app, host, port, ssl_ctx, static_dir):
    runner = web.AppRunner(build_app(vault_app, static_dir), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port, ssl_context=ssl_ctx).start()
    return runner
