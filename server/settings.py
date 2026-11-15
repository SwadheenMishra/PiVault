"""Settings file (config.ini): invite code, web address and storage limits.

The file is plain text so a family admin can edit it in Notepad. The server
re-reads it whenever it changes, so a new invite code or a new storage limit
takes effect without restarting.
"""
import configparser
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("pivault")

FILENAME = "config.ini"
OLD_FILENAME = "config.json"
DEFAULT_STORAGE = "10 GB"
UNITS = {"mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mb|gb|tb)\s*$", re.IGNORECASE)

TEMPLATE = """\
# PiVault settings. Edit this file with any text editor.
# Changes to the invite code and storage limits apply within a few seconds,
# no restart needed. (web_host needs a restart.)
# Sizes need a unit: MB, GB or TB, for example  50 GB  or  1.5 TB.

[pivault]
# People need this code to create an account. Change it any time.
invite_code =

# 127.0.0.1 = only this computer (plus Tailscale/tunnels)
# 0.0.0.0   = other devices on your home network too
web_host =

# Storage each member gets unless they have their own line below.
default_storage = 10 GB

[storage_per_member]
# One line per person who should get something different, for example:
# alice = 50 GB
# bob = 500 MB
"""


class SettingsError(Exception):
    """The settings file has a mistake (the message says where)."""


def parse_size(text):
    m = _SIZE.match(str(text))
    if not m:
        raise ValueError(f"'{str(text).strip()}' is not a size; write a number and a unit, like 10 GB, 500 MB or 1.5 TB")
    value = int(float(m.group(1)) * UNITS[m.group(2).lower()])
    if value <= 0:
        raise ValueError("size must be bigger than 0")
    return value


def format_size(n):
    for unit, mult in (("TB", UNITS["tb"]), ("GB", UNITS["gb"]), ("MB", UNITS["mb"])):
        if n >= mult:
            v = n / mult
            return f"{v:g} {unit}" if v == int(v) else f"{v:.1f} {unit}"
    return f"{n} B"


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)  # holds the invite code: owner-only where supported
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_key(text, key, value):
    """Replace `key = ...` inside [pivault], or insert it, keeping comments and other sections."""
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip().lower()
        if s == "[pivault]":
            start = i
        elif start is not None and s.startswith("[") and s.endswith("]"):
            end = i
            break
    new = f"{key} = {value}"
    if start is None:
        return text.rstrip("\n") + f"\n\n[pivault]\n{new}\n"
    end = len(lines) if end is None else end
    for i in range(start + 1, end):
        if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i], re.IGNORECASE):
            lines[i] = new
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, new)
    return "\n".join(lines) + "\n"


def write_settings(path, **values):
    """Create the file from the template, or change only the given keys."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        text = TEMPLATE
    for key, value in values.items():
        text = _set_key(text, key, value)
    _atomic_write(path, text)
    return path


class Settings:
    """Live view of config.ini. Call reload() before reading; it only re-parses
    when the file changed. A typo made while running keeps the last good values."""

    def __init__(self, path):
        self.path = Path(path)
        self.invite_code = ""
        self.web_host = ""
        self.default_quota = None  # bytes, None = use the built-in default
        self.overrides = {}  # lowercase username -> bytes
        self._sig = None
        self._lock = threading.Lock()
        self._warned = None

    def _signature(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload(self, strict=False):
        with self._lock:
            sig = self._signature()
            if sig == self._sig:
                return
            if sig is None:
                self._sig = sig
                return
            try:
                values = self._parse()
            except SettingsError as e:
                if strict:
                    raise
                self._sig = sig
                if self._warned != str(e):
                    self._warned = str(e)
                    log.warning("%s -- keeping the previous settings until it is fixed", e)
                return
            self._sig = sig
            self._warned = None
            self.invite_code, self.web_host, self.default_quota, self.overrides = values

    def _parse(self):
        name = self.path.name
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",),
                                       comment_prefixes=("#", ";"), strict=False)
        cp.optionxform = str.lower
        try:
            cp.read_string(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, configparser.Error) as e:
            raise SettingsError(f"Cannot read {name}: {e}")
        sec = cp["pivault"] if cp.has_section("pivault") else {}
        get = lambda k: (sec.get(k, "") if sec else "").strip()
        default = None
        if get("default_storage"):
            try:
                default = parse_size(get("default_storage"))
            except ValueError as e:
                raise SettingsError(f"{name}: default_storage: {e}")
        overrides = {}
        if cp.has_section("storage_per_member"):
            for member, raw in cp.items("storage_per_member"):
                try:
                    overrides[member.strip().lower()] = parse_size(raw)
                except ValueError as e:
                    raise SettingsError(f"{name}: storage for '{member}': {e}")
        return get("invite_code"), get("web_host"), default, overrides


def open_settings(data_dir):
    """Load <data_dir>/config.ini, migrating an older config.json if present."""
    data_dir = Path(data_dir)
    path = data_dir / FILENAME
    old = data_dir / OLD_FILENAME
    if not path.exists() and old.exists():
        try:
            import json
            cfg = json.loads(old.read_text(encoding="utf-8"))
            values = {k: str(cfg[k]) for k in ("invite_code", "web_host") if isinstance(cfg.get(k), str)}
            write_settings(path, **values)
            old.replace(data_dir / (OLD_FILENAME + ".migrated"))
            log.info("Moved your saved settings from %s to %s", OLD_FILENAME, FILENAME)
        except (OSError, ValueError):
            pass
    s = Settings(path)
    s.reload(strict=True)
    return s
