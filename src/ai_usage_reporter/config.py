"""The TOML config file and the per-user directories.

Locations (each can be moved with an environment variable):

=========  ===========================================  =========================
What       Default                                      Override
=========  ===========================================  =========================
config     macOS ``~/Library/Application Support/...``  ``AI_USAGE_CONFIG_DIR``
           Windows ``%APPDATA%\\ai-usage-reporter``
           else ``$XDG_CONFIG_HOME/ai-usage-reporter``
state      macOS ``~/Library/Application Support/...``  ``AI_USAGE_STATE_DIR``
           Windows ``%LOCALAPPDATA%\\ai-usage-reporter``
           else ``$XDG_STATE_HOME/ai-usage-reporter``
=========  ===========================================  =========================

See ``docs/configuration.md`` for every setting.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ai_usage_reporter.record import COST_TYPES

APP_DIR = "ai-usage-reporter"
CONFIG_FILE = "config.toml"


class ConfigError(ValueError):
    pass


def is_loopback_host(host: str | None) -> bool:
    """``localhost``, ``127.0.0.0/8`` or ``::1``."""
    if not host:
        return False
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_server_url(url: str, *, allow_insecure_http: bool = False) -> str:
    """The server URL, refused unless the API key would travel encrypted.

    ``https://`` is always accepted. Plain ``http://`` sends the API key in
    clear text, so it is accepted only for this machine (``localhost``,
    ``127.0.0.0/8``, ``::1``), or anywhere when ``allow_insecure_http`` is set
    (off by default; meant for a test server on a trusted private network).
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConfigError("url must be an https:// URL with a host name")
    if parsed.username or parsed.password:
        raise ConfigError("url must not contain a user name or password")
    if parsed.scheme == "http" and not allow_insecure_http and not is_loopback_host(parsed.hostname):
        raise ConfigError(
            "url must use https:// (plain http:// would send the API key unencrypted); http:// is allowed only"
            " for localhost, or with allow_insecure_http = true on a trusted private network"
        )
    return url.strip()


def _platform_dir(env: Mapping[str, str], home: Path, *, state: bool) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR
    if sys.platform.startswith("win"):
        base = env.get("LOCALAPPDATA" if state else "APPDATA")
        return Path(base) / APP_DIR if base else home / "AppData" / ("Local" if state else "Roaming") / APP_DIR
    if state:
        xdg = env.get("XDG_STATE_HOME")
        return (Path(xdg) if xdg else home / ".local" / "state") / APP_DIR
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / APP_DIR


def config_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("AI_USAGE_CONFIG_DIR")
    return Path(override) if override else _platform_dir(env, home or Path.home(), state=False)


def state_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("AI_USAGE_STATE_DIR")
    return Path(override) if override else _platform_dir(env, home or Path.home(), state=True)


@dataclass(frozen=True, slots=True)
class CostRule:
    """``[[cost_rules]]``: the first rule whose filters all match sets the cost hints."""

    cost_type: str | None = None
    subscription_hint: str | None = None
    tool: str | None = None
    model: str | None = None
    repo: str | None = None

    def matches(self, tool: str, model: str, repo: str | None) -> bool:
        if self.tool is not None and not fnmatchcase(tool, self.tool):
            return False
        if self.model is not None and not fnmatchcase(model.lower(), self.model.lower()):
            return False
        if self.repo is not None and (repo is None or not fnmatchcase(repo.lower(), self.repo.lower())):
            return False
        return True


@dataclass(frozen=True, slots=True)
class SourceSettings:
    enabled: bool = True
    roots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    url: str | None = None
    allow_insecure_http: bool = False
    git_enrichment: bool = True
    timezone: str | None = None
    on_behalf_of: str | None = None
    git_commits: bool = True
    exclude_paths: tuple[str, ...] = ()
    sources: Mapping[str, SourceSettings] = field(default_factory=dict)
    cost_rules: tuple[CostRule, ...] = ()
    projects: Mapping[str, str] = field(default_factory=dict)
    long_context: Mapping[str, int] = field(default_factory=dict)
    path: Path | None = None

    def source(self, name: str) -> SourceSettings:
        return self.sources.get(name, SourceSettings())

    def cost_for(self, tool: str, model: str, repo: str | None) -> CostRule | None:
        for rule in self.cost_rules:
            if rule.matches(tool, model, repo):
                return rule
        return None

    def project_for(self, repo: str | None) -> str | None:
        if repo is None:
            return None
        for pattern, project in self.projects.items():
            if fnmatchcase(repo.lower(), pattern.lower()):
                return project
        return None


def _opt_str(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}{key} must be a non-empty string")
    return value.strip()


def _str_list(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}{key} must be a list of strings")
    return tuple(value)


def parse_config(data: Mapping[str, Any], path: Path | None = None) -> Config:
    known = {
        "url", "allow_insecure_http", "timezone", "on_behalf_of", "git_enrichment", "git_commits", "exclude_paths",
        "sources", "cost_rules", "projects", "long_context",
    }  # fmt: skip
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")
    flags: dict[str, bool] = {}
    for name, default in (("git_commits", True), ("git_enrichment", True), ("allow_insecure_http", False)):
        value = data.get(name, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        flags[name] = value
    sources: dict[str, SourceSettings] = {}
    raw_sources = data.get("sources", {})
    if not isinstance(raw_sources, dict):
        raise ConfigError("[sources] must be a table")
    for name, table in raw_sources.items():
        if not isinstance(table, dict):
            raise ConfigError(f"[sources.{name}] must be a table")
        enabled = table.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"sources.{name}.enabled must be true or false")
        sources[name] = SourceSettings(enabled=enabled, roots=_str_list(table, "roots", f"sources.{name}."))
    rules: list[CostRule] = []
    raw_rules = data.get("cost_rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("cost_rules must be an array of tables ([[cost_rules]])")
    for number, rule in enumerate(raw_rules, start=1):
        if not isinstance(rule, dict):
            raise ConfigError(f"cost_rules #{number} must be a table")
        extra = set(rule) - {"cost_type", "subscription_hint", "tool", "model", "repo"}
        if extra:
            raise ConfigError(f"cost_rules #{number}: unknown key(s) {', '.join(sorted(extra))}")
        where = f"cost_rules #{number}: "
        cost_type = _opt_str(rule, "cost_type", where)
        if cost_type is not None and cost_type not in COST_TYPES:
            raise ConfigError(f"{where}cost_type must be one of {', '.join(COST_TYPES)}")
        rules.append(
            CostRule(
                cost_type=cost_type,
                subscription_hint=_opt_str(rule, "subscription_hint", where),
                tool=_opt_str(rule, "tool", where),
                model=_opt_str(rule, "model", where),
                repo=_opt_str(rule, "repo", where),
            )
        )
    projects = data.get("projects", {})
    if not isinstance(projects, dict) or not all(isinstance(v, str) for v in projects.values()):
        raise ConfigError("[projects] maps repository patterns to project keys (strings)")
    long_context = data.get("long_context", {})
    if not isinstance(long_context, dict) or not all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in long_context.values()
    ):
        raise ConfigError("[long_context] maps model patterns to positive token thresholds")
    url = _opt_str(data, "url", "")
    if url is not None:
        url = check_server_url(url, allow_insecure_http=flags["allow_insecure_http"])
    return Config(
        url=url,
        allow_insecure_http=flags["allow_insecure_http"],
        git_enrichment=flags["git_enrichment"],
        timezone=_opt_str(data, "timezone", ""),
        on_behalf_of=_opt_str(data, "on_behalf_of", ""),
        git_commits=flags["git_commits"],
        exclude_paths=_str_list(data, "exclude_paths", ""),
        sources=sources,
        cost_rules=tuple(rules),
        projects=dict(projects),
        long_context=dict(long_context),
        path=path,
    )


def load_config(path: Path) -> Config:
    """The config at *path*; a missing file is an empty config."""
    if not path.exists():
        return Config(path=path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    return parse_config(data, path)


CONFIG_TEMPLATE = """\
# ai-usage-reporter configuration. See docs/configuration.md.

# Your Specivo server. The API key is not stored here: it lives in the OS
# keychain, in the SPECIVO_API_KEY environment variable or in the api-key file
# next to this one (readable only by you).
url = "{url}"
{insecure}
# Time zone for dates shown by "ai-usage status". Buckets are always UTC days.
# timezone = "Asia/Bangkok"

# A service account reporting for one person sends their login.
# on_behalf_of = "login"

# Ask git (read-only, hardened) for the repository and the commits of a session.
# Set to false to never run git; "push --no-git" does the same for one run.
git_enrichment = true

# Add the SHAs of commits you made during a session (needs git_enrichment).
git_commits = true

# Sessions that ran in these directories are never reported.
exclude_paths = []

# [sources.claude-code]
# enabled = true
# roots = ["~/.claude"]

# [sources.codex]
# enabled = true

# How usage is paid. The first matching rule wins; leave out to let Specivo decide.
# [[cost_rules]]
# tool = "claude-code"
# cost_type = "subscription"
# subscription_hint = "claude-max-20x"

# Repository (host/owner/repo, patterns allowed) -> Specivo project key.
[projects]
# "github.com/acme/*" = "ACME"

# Models with a long-context price tier: model (patterns allowed) -> threshold in tokens.
[long_context]
# "example-model-*" = 200000
"""


def render_template(url: str, *, allow_insecure_http: bool = False) -> str:
    insecure = (
        "# Plain http:// to a host that is not this machine: the API key travels unencrypted.\n"
        "allow_insecure_http = true\n"
        if allow_insecure_http
        else ""
    )
    escaped = url.replace("\\", "\\\\").replace('"', '\\"')
    return CONFIG_TEMPLATE.replace("{url}", escaped).replace("{insecure}", insecure)
