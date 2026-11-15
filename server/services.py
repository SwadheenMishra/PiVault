"""Logic shared by the TCP server and the HTTP (web UI) server."""
import asyncio
import hmac
import logging
import time
from collections import defaultdict
from pathlib import Path

from auth import UserDB
from protocol import ServiceError
from settings import format_size
from storage import Storage

log = logging.getLogger("pivault")

DEFAULT_QUOTA_BYTES = 10 * 1024**3


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
    def __init__(self, data_dir, settings, explicit_invite="", explicit_quota=None):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._explicit_invite = explicit_invite
        self._explicit_quota = explicit_quota
        self.users = UserDB(data_dir / "users.db")
        self.storage = Storage(data_dir, self.quota_for)
        self.limiter = RateLimiter()
        self.active = 0

    @property
    def invite_code(self):
        """A --invite-code flag or env var wins; otherwise the live settings file."""
        if self._explicit_invite:
            return self._explicit_invite
        self.settings.reload()
        return self.settings.invite_code

    def default_quota(self):
        self.settings.reload()
        return self._explicit_quota or self.settings.default_quota or DEFAULT_QUOTA_BYTES

    def quota_for(self, username):
        """A member's own line in [storage_per_member] wins over the default."""
        self.settings.reload()
        return self.settings.overrides.get(username.lower()) or self.default_quota()

    def describe_quotas(self):
        text = format_size(self.default_quota())
        extra = ", ".join(f"{u} {format_size(b)}" for u, b in sorted(self.settings.overrides.items()))
        return f"{text}  ({extra})" if extra else text


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
