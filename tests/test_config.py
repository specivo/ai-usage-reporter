from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

from ai_usage_reporter.apikey import ApiKey, KeyFileError, resolve_api_key, store_api_key
from ai_usage_reporter.config import ConfigError, config_dir, load_config, parse_config, render_template, state_dir


def test_the_template_parses() -> None:
    config = parse_config(tomllib.loads(render_template("https://specivo.example.com")))
    assert config.url == "https://specivo.example.com"
    assert config.git_commits is True


def test_a_full_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
url = "https://specivo.example.com"
timezone = "Asia/Bangkok"
on_behalf_of = "teammate"
exclude_paths = ["~/private"]

[sources.codex]
enabled = false
roots = ["~/other-codex"]

[[cost_rules]]
repo = "github.com/acme/*"
cost_type = "api"

[[cost_rules]]
tool = "claude-code"
cost_type = "subscription"
subscription_hint = "claude-pro"

[projects]
"github.com/acme/*" = "ACME"

[long_context]
"big-model-*" = 200000
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.source("codex").enabled is False and config.source("codex").roots == ("~/other-codex",)
    assert config.source("claude-code").enabled is True
    rule = config.cost_for("claude-code", "m", "github.com/acme/app")
    assert rule is not None and rule.cost_type == "api"
    rule = config.cost_for("claude-code", "m", None)
    assert rule is not None and rule.subscription_hint == "claude-pro"
    assert config.project_for("GitHub.com/acme/app") == "ACME" and config.project_for(None) is None


@pytest.mark.parametrize(
    "text",
    [
        "surprise = 1",
        'url = "ftp://x"',
        '[[cost_rules]]\ncost_type = "cheap"',
        "[[cost_rules]]\nwhat = 1",
        '[long_context]\n"m" = -1',
        'git_commits = "yes"',
    ],
)
def test_bad_configs_are_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(tomllib.loads(text))


def test_missing_config_is_empty(tmp_path: Path) -> None:
    assert load_config(tmp_path / "none.toml").url is None


def test_directories_can_be_moved(tmp_path: Path) -> None:
    env = {"AI_USAGE_CONFIG_DIR": str(tmp_path / "c"), "AI_USAGE_STATE_DIR": str(tmp_path / "s")}
    assert config_dir(env) == tmp_path / "c" and state_dir(env) == tmp_path / "s"


def test_key_order_environment_then_file(tmp_path: Path) -> None:
    assert resolve_api_key(None, tmp_path, {}) is None
    assert store_api_key("https://h.example", "spv_file", tmp_path, use_keyring=False) == "file"
    key = resolve_api_key(None, tmp_path, {})
    assert key == ApiKey("spv_file", "file")
    assert resolve_api_key(None, tmp_path, {"SPECIVO_API_KEY": "spv_env"}) == ApiKey("spv_env", "environment")
    assert "spv_" not in repr(key)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes")
def test_a_readable_key_file_is_refused(tmp_path: Path) -> None:
    store_api_key("https://h.example", "spv_file", tmp_path, use_keyring=False)
    path = tmp_path / "api-key"
    assert (path.stat().st_mode & 0o777) == 0o600
    os.chmod(path, 0o644)
    with pytest.raises(KeyFileError):
        resolve_api_key(None, tmp_path, {})


def test_keychain_comes_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import ai_usage_reporter.apikey as apikey

    monkeypatch.setattr(apikey, "_keyring_get", lambda url: "spv_keychain")
    key = resolve_api_key("https://h.example", tmp_path, {"SPECIVO_API_KEY": "spv_env"})
    assert key == ApiKey("spv_keychain", "keychain")


def test_a_source_project_applies_when_no_repository_pattern_matches(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[projects]
"github.com/acme/*" = "ACME"

[sources.opencode]
project = "WORK"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.project_for(None, "opencode") == "WORK"
    assert config.project_for("github.com/other/app", "opencode") == "WORK"
    assert config.project_for("github.com/acme/app", "opencode") == "ACME"
    assert config.project_for(None, "codex") is None
