"""Finding the Specivo API key: the OS keychain, then ``SPECIVO_API_KEY``, then a private file.

The key is never written to the config file, never printed and never logged.
The file (``api-key`` in the config directory) is used only when no other
account on the machine can read it (mode ``0600`` on POSIX systems).
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

KEYRING_SERVICE = "ai-usage-reporter"
KEY_FILE = "api-key"
ENV_VAR = "SPECIVO_API_KEY"


class KeyFileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApiKey:
    value: str
    source: str  # "keychain", "environment" or "file"

    def __repr__(self) -> str:  # never show the key itself
        return f"ApiKey(source={self.source!r})"


def _keyring_get(url: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        value = keyring.get_password(KEYRING_SERVICE, url)
    except Exception:  # no usable backend: fall through to the next source
        return None
    return value if isinstance(value, str) and value.strip() else None


def keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
    except ImportError:
        return False
    return True


def file_is_private(path: Path) -> bool:
    if sys.platform.startswith("win"):
        return True  # the user profile directory is private by default
    mode = path.stat().st_mode
    return not mode & (stat.S_IRWXG | stat.S_IRWXO)


def resolve_api_key(url: str | None, config_directory: Path, env: Mapping[str, str] | None = None) -> ApiKey | None:
    env = os.environ if env is None else env
    if url:
        from_keyring = _keyring_get(url)
        if from_keyring:
            return ApiKey(from_keyring.strip(), "keychain")
    from_env = env.get(ENV_VAR, "").strip()
    if from_env:
        return ApiKey(from_env, "environment")
    path = config_directory / KEY_FILE
    if path.exists():
        if not file_is_private(path):
            raise KeyFileError(
                f"the {KEY_FILE} file can be read by other users; run: chmod 600 <config dir>/{KEY_FILE}"
            )
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return ApiKey(value, "file")
    return None


def store_api_key(url: str, key: str, config_directory: Path, *, use_keyring: bool = True) -> str:
    """Store *key* in the keychain when possible, else in a ``0600`` file. Returns where."""
    if use_keyring and keyring_available():
        import keyring

        try:
            keyring.set_password(KEYRING_SERVICE, url, key)
            return "keychain"
        except Exception:
            pass
    config_directory.mkdir(parents=True, exist_ok=True)
    path = config_directory / KEY_FILE
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key.strip() + "\n")
    if not sys.platform.startswith("win"):
        os.chmod(path, 0o600)
    return "file"
