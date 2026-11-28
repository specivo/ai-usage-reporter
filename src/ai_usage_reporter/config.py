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

See ``docs/index.md`` for every setting.
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
from pathlib import Path, PurePath
from typing import Any

from ai_usage_reporter.activity_record import BUILT_LEVEL, COUNTS, DEFAULT_LEVEL, LEVELS, lowest_level
from ai_usage_reporter.record import BILLING_ACCOUNT, COST_TYPES

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
    """``[[cost_rules]]``: the first rule whose filters all match sets the cost hints.

    Filters (all optional; a rule without filters matches everything):

    - ``tool``: glob, case-sensitive (tool slugs are lowercase).
    - ``model``, ``repo``, ``route``: glob, case-insensitive. ``repo`` is
      ``host/owner/repo``; ``route`` is the backend the tool says the requests
      went through (Codex ``model_provider``, OpenCode ``providerID``, the
      Claude Code base URL host). A record without a repo or route never
      matches a rule that names one.
    - ``cwd``: glob on the working folder, ``~`` expanded when the config is
      loaded. It matches the folder itself or any folder above it, so a plain
      path matches everything under it, like ``exclude_paths``. Case follows
      the platform (``os.path.normcase``: insensitive on Windows only). A bucket
      that ran in several folders is matched by the one most of its requests
      ran in, the same folder its repository is taken from.

    Outputs: ``cost_type``, ``subscription_hint`` and ``billing_account`` (a
    Specivo billing account slug, resolved by the server against the person's
    own and the instance's accounts).
    """

    cost_type: str | None = None
    subscription_hint: str | None = None
    billing_account: str | None = None
    tool: str | None = None
    model: str | None = None
    repo: str | None = None
    route: str | None = None
    cwd: str | None = None

    def matches(
        self,
        tool: str,
        model: str,
        repo: str | None,
        *,
        route: str | None = None,
        cwd: str | None = None,
    ) -> bool:
        if self.tool is not None and not fnmatchcase(tool, self.tool):
            return False
        if self.model is not None and not fnmatchcase(model.lower(), self.model.lower()):
            return False
        if self.repo is not None and (repo is None or not fnmatchcase(repo.lower(), self.repo.lower())):
            return False
        if self.route is not None and (route is None or not fnmatchcase(route.lower(), self.route.lower())):
            return False
        if self.cwd is not None and (cwd is None or not cwd_matches(cwd, self.cwd)):
            return False
        return True


def cwd_matches(cwd: str, pattern: str) -> bool:
    """Whether the folder *cwd*, or a folder above it, matches the glob *pattern*."""
    folder = PurePath(os.path.normcase(cwd))
    wanted = os.path.normcase(pattern)
    if len(wanted) > 1:
        wanted = wanted.rstrip("/\\")
    return any(fnmatchcase(str(candidate), wanted) for candidate in (folder, *folder.parents))


#: The keys of a ``[[cost_rules]]`` entry: its outputs, then its filters.
COST_RULE_KEYS = frozenset(
    {"cost_type", "subscription_hint", "billing_account", "tool", "model", "repo", "route", "cwd"}
)

#: Where a tool's usage comes from: its own logs (``native``) or ``ai-usage import ccusage``.
SOURCE_VIAS = ("native", "ccusage")


@dataclass(frozen=True, slots=True)
class SourceSettings:
    """``[sources.<tool>]``.

    ``via`` makes the two ways of reading a tool exclusive: with ``native`` (the
    default) ``push`` reads the tool's logs and ``import ccusage`` skips its
    rows; with ``ccusage`` it is the other way round. A tool is never counted
    twice. ``project`` is the Specivo project key for this tool's usage when no
    ``[projects]`` repository pattern matches (for example a tool used outside
    any git repository).
    """

    enabled: bool = True
    roots: tuple[str, ...] = ()
    via: str = "native"
    project: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    url: str | None = None
    allow_insecure_http: bool = False
    git_enrichment: bool = True
    timezone: str | None = None
    on_behalf_of: str | None = None
    git_commits: bool = True
    #: Split sessions by the issues the agent marked with its Specivo tool calls, and send them.
    agent_log_issues: bool = True
    #: Send the measured active time (seconds and spans) of sessions whose tool logs it.
    active_time: bool = True
    exclude_paths: tuple[str, ...] = ()
    sources: Mapping[str, SourceSettings] = field(default_factory=dict)
    cost_rules: tuple[CostRule, ...] = ()
    projects: Mapping[str, str] = field(default_factory=dict)
    long_context: Mapping[str, int] = field(default_factory=dict)
    #: ``[activity] level``: how much work activity to send (``off``, ``counts``, ``names``, ``commits``).
    activity_level: str = DEFAULT_LEVEL
    #: ``[activity.repos]``: repository pattern -> level; the first match wins.
    activity_repos: Mapping[str, str] = field(default_factory=dict)
    path: Path | None = None

    def source(self, name: str) -> SourceSettings:
        return self.sources.get(name, SourceSettings())

    def cost_for(
        self,
        tool: str,
        model: str,
        repo: str | None,
        *,
        route: str | None = None,
        cwd: str | None = None,
    ) -> CostRule | None:
        for rule in self.cost_rules:
            if rule.matches(tool, model, repo, route=route, cwd=cwd):
                return rule
        return None

    def project_for(self, repo: str | None, tool: str | None = None) -> str | None:
        """The project of a repository by ``[projects]``; otherwise the tool's ``[sources.<tool>] project``."""
        if repo is not None:
            for pattern, project in self.projects.items():
                if fnmatchcase(repo.lower(), pattern.lower()):
                    return project
        if tool is not None:
            return self.source(tool).project
        return None

    def activity_level_for(self, repo: str | None) -> str:
        """The configured work activity level of a session in *repo* (``host/owner/repo``, ``None``: unknown).

        The first ``[activity.repos]`` pattern matching the repository wins (patterns match as in
        ``[projects]``), else ``[activity] level``. A session whose repository is unknown never gets more
        than ``counts``.
        """
        if repo is not None:
            for pattern, level in self.activity_repos.items():
                if fnmatchcase(repo.lower(), pattern.lower()):
                    return level
            return self.activity_level
        return lowest_level(self.activity_level, COUNTS)

    def activity_sent_level(self, repo: str | None) -> str:
        """The level records of a session in *repo* are built at: the configured one, at most what is built."""
        return lowest_level(self.activity_level_for(repo), BUILT_LEVEL)


def _parse_activity(data: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    table = data.get("activity", {})
    if not isinstance(table, dict):
        raise ConfigError("[activity] must be a table")
    extra = set(table) - {"level", "repos"}
    if extra:
        raise ConfigError(f"[activity]: unknown key(s) {', '.join(sorted(extra))}")
    level = table.get("level", DEFAULT_LEVEL)
    if level not in LEVELS:
        raise ConfigError(f"activity.level must be one of {', '.join(LEVELS)}")
    repos = table.get("repos", {})
    if not isinstance(repos, dict):
        raise ConfigError("[activity.repos] maps repository patterns to levels")
    for pattern, value in repos.items():
        if value not in LEVELS:
            raise ConfigError(f"[activity.repos] {pattern!r}: the level must be one of {', '.join(LEVELS)}")
    return level, dict(repos)


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
        "agent_log_issues", "active_time", "sources", "cost_rules", "projects", "long_context", "activity",
    }  # fmt: skip
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")
    flags: dict[str, bool] = {}
    for name, default in (
        ("git_commits", True),
        ("git_enrichment", True),
        ("allow_insecure_http", False),
        ("agent_log_issues", True),
        ("active_time", True),
    ):
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
        extra = set(table) - {"enabled", "roots", "via", "project"}
        if extra:
            raise ConfigError(f"[sources.{name}]: unknown key(s) {', '.join(sorted(extra))}")
        via = table.get("via", "native")
        if via not in SOURCE_VIAS:
            raise ConfigError(f"sources.{name}.via must be one of {', '.join(SOURCE_VIAS)}")
        sources[name] = SourceSettings(
            enabled=enabled,
            roots=_str_list(table, "roots", f"sources.{name}."),
            via=via,
            project=_opt_str(table, "project", f"sources.{name}."),
        )
    rules: list[CostRule] = []
    raw_rules = data.get("cost_rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("cost_rules must be an array of tables ([[cost_rules]])")
    for number, rule in enumerate(raw_rules, start=1):
        if not isinstance(rule, dict):
            raise ConfigError(f"cost_rules #{number} must be a table")
        extra = set(rule) - COST_RULE_KEYS
        if extra:
            raise ConfigError(f"cost_rules #{number}: unknown key(s) {', '.join(sorted(extra))}")
        where = f"cost_rules #{number}: "
        cost_type = _opt_str(rule, "cost_type", where)
        if cost_type is not None and cost_type not in COST_TYPES:
            raise ConfigError(f"{where}cost_type must be one of {', '.join(COST_TYPES)}")
        billing_account = _opt_str(rule, "billing_account", where)
        if billing_account is not None and not BILLING_ACCOUNT.match(billing_account):
            raise ConfigError(
                f"{where}billing_account {billing_account!r} is not a billing account slug: 1 to 64 characters,"
                " lowercase letters, digits, '.', '_' or '-', starting with a letter or digit"
            )
        cwd = _opt_str(rule, "cwd", where)
        rules.append(
            CostRule(
                cost_type=cost_type,
                subscription_hint=_opt_str(rule, "subscription_hint", where),
                billing_account=billing_account,
                tool=_opt_str(rule, "tool", where),
                model=_opt_str(rule, "model", where),
                repo=_opt_str(rule, "repo", where),
                route=_opt_str(rule, "route", where),
                cwd=str(Path(cwd).expanduser()) if cwd is not None else None,
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
    activity_level, activity_repos = _parse_activity(data)
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
        agent_log_issues=flags["agent_log_issues"],
        active_time=flags["active_time"],
        exclude_paths=_str_list(data, "exclude_paths", ""),
        sources=sources,
        cost_rules=tuple(rules),
        projects=dict(projects),
        long_context=dict(long_context),
        activity_level=activity_level,
        activity_repos=activity_repos,
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
# ai-usage-reporter configuration. See docs/index.md.

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

# Count a long session for each issue the agent marked with its Specivo tool
# calls (status, assignee, timer start, created issue). Only the tool name and
# the issue key are read from the transcript.
agent_log_issues = true

# Send how long the agent was active in each record: the summed seconds and
# the intervals (start and end, to the second) when it worked. Measured from
# the timestamps of prompts, model answers and tool calls; nothing of their
# content is read. Claude Code and Codex.
active_time = true

# Sessions that ran in these directories are never reported.
exclude_paths = []

# [sources.claude-code]
# enabled = true
# roots = ["~/.claude"]

# [sources.codex]
# enabled = true

# [sources.opencode]
# roots = ["~/.local/share/opencode"]
# project = "ACME"   # its project when no [projects] repository pattern matches

# [sources.hermes]
# roots = ["~/.hermes"]   # Hermes homes; each profile in profiles/<name> is read too

# A tool read through "ai-usage import ccusage" instead of its own logs.
# The two are exclusive, so the tool is never counted twice.
# [sources.gemini-cli]
# via = "ccusage"

# How usage is paid and who pays. The first matching rule wins; leave out to let
# Specivo decide. Filters (all optional, patterns allowed): tool, model, repo,
# route (the backend: Codex model_provider, OpenCode providerID, Claude Code
# base URL host) and cwd (a working folder, matching everything under it).
# Outputs: cost_type, subscription_hint and billing_account (a Specivo billing
# account slug). A tool-wide rule also catches gateway usage: scope it by route.
# [[cost_rules]]
# tool = "codex"
# route = "corp-*"
# billing_account = "corp-gateway"
#
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

# Work activity: a summary of what the agent did on this machine in each session (tool runs by
# category and their seconds, files changed, lines added and removed, test runs and failures,
# subagents, a timeline labelled by category). Sent only to a Specivo server that takes it.
# "counts" (the default) sends counts and durations only, never a name, command, path or content;
# "off" sends nothing. "names" and "commits" are accepted but send "counts" for now.
[activity]
level = "counts"

# Repository (host/owner/repo, patterns allowed) -> level; the first match wins. A session whose
# repository is unknown never gets more than "counts".
[activity.repos]
# "github.com/client-x/*" = "off"
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
