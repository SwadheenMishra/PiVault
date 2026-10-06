"""User accounts: SQLite + scrypt password hashing (standard library only)."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing

from protocol import ServiceError

# Lowercase letters, digits, underscore. Must start with a letter/digit.
# This strict whitelist is also what makes the username safe to use as a folder name.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 128
SESSION_TTL = 14 * 24 * 3600  # web sessions last 14 days

# ~16 MB RAM per hash: fine on a Pi, still painful for brute-forcing.
SCRYPT = dict(n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)

_DUMMY_SALT = os.urandom(16)


def normalize_username(username):
    return username.strip().lower()


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


class UserDB:
    def __init__(self, path):
        self.path = str(path)
        with closing(self._conn()) as c, c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS users (
                       username   TEXT PRIMARY KEY,
                       salt       BLOB NOT NULL,
                       pw_hash    BLOB NOT NULL,
                       created_at INTEGER NOT NULL
                   )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                       token_hash TEXT PRIMARY KEY,
                       username   TEXT NOT NULL,
                       expires_at INTEGER NOT NULL
                   )"""
            )
        os.chmod(self.path, 0o600)

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10)

    def create_user(self, username, password):
        """Create an account. Returns the normalized username."""
        username = normalize_username(username)
        if not USERNAME_RE.match(username):
            raise ServiceError(
                "username must be 3-32 characters: a-z, 0-9 and _ only"
            )
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            raise ServiceError(
                f"password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters"
            )
        salt = os.urandom(16)
        pw_hash = _hash(password, salt)
        try:
            with closing(self._conn()) as c, c:
                c.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?)",
                    (username, salt, pw_hash, int(time.time())),
                )
        except sqlite3.IntegrityError:
            raise ServiceError("that username is already taken")
        return username

    def verify(self, username, password):
        """Check credentials. Returns the normalized username or None."""
        username = normalize_username(username)
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT salt, pw_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            _hash(password, _DUMMY_SALT)  # same work for unknown users (timing)
            return None
        salt, expected = row
        ok = hmac.compare_digest(_hash(password, salt), expected)
        return username if ok else None

    # --- web sessions: only a SHA-256 of the token is stored, so a leaked
    # database file does not leak usable login cookies.
    @staticmethod
    def _th(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self, username):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            c.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                      (self._th(token), username, now + SESSION_TTL))
        return token

    def session_user(self, token):
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT username FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (self._th(token), int(time.time())),
            ).fetchone()
        return row[0] if row else None

    def delete_session(self, token):
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (self._th(token),))
