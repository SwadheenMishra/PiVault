"""Logic shared by the TCP server and the HTTP (web UI) server."""
import asyncio
import hmac
import logging
import time
from collections import defaultdict
from pathlib import Path

from auth import UserDB
from protocol import ServiceError
from storage import Storage

log = logging.getLogger("pivault")


class RateLimitError(ServiceError):
    """Too many failed attempts (the web layer maps this to HTTP 429)."""


class RateLimiter:
    """Blocks a key after too many recent failures (brute-force protection)."""

    def __init__(self, max_failures=5, window=300):
        self.max_failures = max_failures
        self.window = window
        self._fails = defaultdict(list)

    def _prune(self, key):
        cutoff = time.monotonic() - self.window
        self._fails[key] = [t for t in self._fails[key] if t > cutoff]
        if not self._fails[key]:
            del self._fails[key]

    def check(self, key):
        self._prune(key)
        if len(self._fails.get(key, ())) >= self.max_failures:
            raise RateLimitError("too many failed attempts, try again in a few minutes")

    def fail(self, key):
        self._fails[key].append(time.monotonic())

    def reset(self, key):
        self._fails.pop(key, None)


class App:
    def __init__(self, data_dir, quota_bytes, invite_code):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.users = UserDB(data_dir / "users.db")
        self.storage = Storage(data_dir, quota_bytes)
        self.limiter = RateLimiter()
        self.invite_code = invite_code
        self.active = 0


def need_str(args, key, maxlen=1024, required=True):
    val = args.get(key, None if required else "")
    if not isinstance(val, str) or (required and not val):
        raise ServiceError(f"missing or invalid '{key}'")
    if len(val) > maxlen:
        raise ServiceError(f"'{key}' is too long")
    return val


async def register_user(app, ip, args):
    """Create an account. Returns the normalized username."""
    ip_key = f"ip:{ip}"
    app.limiter.check(ip_key)
    if app.invite_code:
        given = need_str(args, "invite_code", 128, required=False)
        if not hmac.compare_digest(given.encode(), app.invite_code.encode()):
            app.limiter.fail(ip_key)
            raise ServiceError("invalid invite code")
    username = need_str(args, "username", 64)
    password = need_str(args, "password", 1024)
    username = await asyncio.to_thread(app.users.create_user, username, password)
    await asyncio.to_thread(app.storage.user_root, username)
    log.info("register user=%s ip=%s", username, ip)
    return username


async def login_user(app, ip, args):
    """Check credentials with brute-force protection. Returns the username."""
    username = need_str(args, "username", 64)
    password = need_str(args, "password", 1024)
    keys = (f"ip:{ip}", f"user:{username.strip().lower()}")
    for k in keys:
        app.limiter.check(k)
    user = await asyncio.to_thread(app.users.verify, username, password)
    if user is None:
        for k in keys:
            app.limiter.fail(k)
        log.warning("login FAILED user=%r ip=%s", username[:64], ip)
        raise ServiceError("invalid username or password")
    for k in keys:
        app.limiter.reset(k)
    log.info("login user=%s ip=%s", user, ip)
    return user
