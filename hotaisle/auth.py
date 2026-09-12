"""API key discovery for the Hot Aisle client.

Resolution order (first hit wins), highest precedence first:

Environment variables are considered before the config file, so a shell or a
systemd unit can always override what is on disk.

1. ``api_key=...`` argument passed in code / ``--api-key`` on the CLI.
2. ``HOTAISLE_API_KEY`` (or ``HOTAISLE_TOKEN``) environment variable.
3. ``HOTAISLE_API_KEY_FILE`` environment variable -> path to a file holding the key.
4. ``HOTAISLE_API_KEY_COMMAND`` environment variable -> shell command whose stdout
   is the key (e.g. ``pass show hotaisle``, ``op read ...``, ``aws secretsmanager ...``).
5. The config file (default ``~/.config/hotaisle/config.toml``), trying in order:
   ``key_command``, then ``key_file``, then an inline ``api_key``.
6. The OS keyring, if the optional ``keyring`` package is installed *and*
   ``HOTAISLE_KEYRING=1`` (or ``use_keyring = true`` in the config).

The key is never printed or logged; use :func:`mask` if you need to show it.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import ConfigurationError

ENV_KEY = "HOTAISLE_API_KEY"
ENV_TOKEN = "HOTAISLE_TOKEN"
ENV_KEY_FILE = "HOTAISLE_API_KEY_FILE"
ENV_KEY_COMMAND = "HOTAISLE_API_KEY_COMMAND"
ENV_KEYRING = "HOTAISLE_KEYRING"
ENV_CONFIG = "HOTAISLE_CONFIG"
ENV_BASE_URL = "HOTAISLE_BASE_URL"

KEYRING_SERVICE = "hotaisle"
DEFAULT_CONFIG_PATHS = (
    "~/.config/hotaisle/config.toml",
    "~/.config/hotaisle/config",
)


def default_config_path() -> Path:
    """Where the config file lives, honouring ``$XDG_CONFIG_HOME``."""
    override = os.environ.get(ENV_CONFIG)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "hotaisle" / "config.toml"
    return Path(DEFAULT_CONFIG_PATHS[0]).expanduser()


def normalize_key(raw: str) -> str:
    """Accept a bare key or a preformatted ``Token <key>`` value and return the bare key."""
    key = (raw or "").strip().strip('"').strip("'")
    for prefix in ("Token ", "token ", "Bearer ", "bearer "):
        if key.startswith(prefix):
            return key[len(prefix):].strip()
    return key


def mask(key: str, keep: int = 4) -> str:
    """Redact a secret, leaving only a short identifying prefix."""
    if not key:
        return "<empty>"
    if len(key) <= keep:
        return "*" * len(key)
    return "%s%s" % (key[:keep], "*" * min(len(key) - keep, 12))


def load_config(path: Optional[Path] = None) -> dict:
    """Load the optional config file. Missing file -> ``{}``; broken file -> clear error."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if not cfg_path.is_file():
        return {}
    text = cfg_path.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - only on <3.11
        return _parse_mini_toml(text)
    try:
        return tomllib.loads(text)
    except Exception as exc:  # pragma: no cover - depends on user config
        raise ConfigurationError(
            "Could not parse config file %s: %s" % (cfg_path, exc)
        ) from exc


def _parse_mini_toml(text: str) -> dict:
    """Very small fallback parser: ``key = "value"`` lines, ``[section]`` ignored."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def warn_if_world_readable(path: Path) -> Optional[str]:
    """Return a warning string if the config file is readable by others."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return ("config file %s is readable by group/others (mode %o) - "
                "run: chmod 600 %s" % (path, mode, path))
    return None


@dataclass
class Credential:
    """A resolved API key plus a note about where it came from."""

    api_key: str
    source: str

    def __repr__(self) -> str:  # never leak the key via repr
        return "Credential(api_key=%r, source=%r)" % (mask(self.api_key), self.source)


def _read_key_file(raw_path: str, label: str) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ConfigurationError("%s points at %s which does not exist" % (label, path))
    key = normalize_key(path.read_text(encoding="utf-8"))
    if not key:
        raise ConfigurationError("%s (%s) is empty" % (label, path))
    warning = warn_if_world_readable(path)
    if warning:
        import warnings

        warnings.warn(warning, RuntimeWarning, stacklevel=2)
    return key


def _run_key_command(command: str) -> str:
    try:
        proc = subprocess.run(
            shlex.split(command) if os.name != "nt" else command,
            capture_output=True, text=True, timeout=30, shell=os.name == "nt",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(
            "key_command %r failed to run: %s" % (command, exc)
        ) from exc
    if proc.returncode != 0:
        raise ConfigurationError(
            "key_command %r exited %d: %s"
            % (command, proc.returncode, (proc.stderr or "").strip()[:300])
        )
    value = normalize_key(proc.stdout)
    if not value:
        raise ConfigurationError("key_command %r printed no API key" % command)
    return value


def _from_keyring() -> Optional[str]:
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, os.environ.get("USER", "hotaisle"))
    except Exception:
        return None


def resolve_api_key(
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
    allow_keyring: Optional[bool] = None,
) -> Credential:
    """Find an API key, raising :class:`ConfigurationError` with instructions if absent."""
    if api_key:
        return Credential(normalize_key(api_key), "explicit argument")

    env_key = os.environ.get(ENV_KEY) or os.environ.get(ENV_TOKEN)
    if env_key:
        return Credential(normalize_key(env_key), "environment variable %s" % ENV_KEY)

    env_file = os.environ.get(ENV_KEY_FILE)
    if env_file:
        return Credential(_read_key_file(env_file, ENV_KEY_FILE),
                          "file from %s" % ENV_KEY_FILE)

    env_command = os.environ.get(ENV_KEY_COMMAND)
    if env_command:
        return Credential(_run_key_command(env_command), "HOTAISLE_API_KEY_COMMAND")

    # Environment has been exhausted; fall through to the config file, whose own
    # keys are tried in the same order (command, then file, then inline value).
    cfg = config if config is not None else load_config()
    if cfg.get("key_command"):
        return Credential(_run_key_command(cfg["key_command"]), "key_command in config")
    if cfg.get("key_file"):
        return Credential(_read_key_file(cfg["key_file"], "key_file"),
                          "key_file in config")
    if cfg.get("api_key"):
        return Credential(normalize_key(cfg["api_key"]),
                          "config file %s" % default_config_path())

    want_keyring = allow_keyring
    if want_keyring is None:
        want_keyring = (
            os.environ.get(ENV_KEYRING, "").lower() in ("1", "true", "yes")
            or bool(cfg.get("use_keyring"))
        )
    if want_keyring:
        keyed = _from_keyring()
        if keyed:
            return Credential(normalize_key(keyed), "OS keyring")

    raise ConfigurationError(NO_KEY_HINT)


NO_KEY_HINT = """\
No Hot Aisle API key found. Provide one in any of these ways:

  1. Environment variable (simplest):
       export HOTAISLE_API_KEY='your-key'
     Add it to your shell profile, or use systemd's Environment= / a
     systemd drop-in, or pass it on a single command line.

  2. Key file, good for containers and cron (chmod 600 it):
       export HOTAISLE_API_KEY_FILE=~/.config/hotaisle/api_key

  3. A command that prints the key, good for pass/1Password/vault:
       export HOTAISLE_API_KEY_COMMAND='pass show hotaisle/api'
     or key_command = "op read op://personal/hotaisle/api" in the config file.

  4. Config file (~/.config/hotaisle/config.toml, chmod 600):
       api_key = "your-key"
       team  = "your-team-handle"

Create/review keys at https://admin.hotaisle.app/ or via:
  hotaisle api-keys      (GET /user/api_keys/)
"""
