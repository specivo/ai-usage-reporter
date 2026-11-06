"""The API key only travels encrypted and never follows a redirect; git never runs repository programs."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter import cli
from ai_usage_reporter.config import Config, ConfigError, check_server_url, parse_config, render_template
from ai_usage_reporter.enrich import GIT_SAFETY_ENV, GIT_SAFETY_OPTIONS, GitInfo, run_git
from ai_usage_reporter.sinks.specivo import SinkError, SpecivoSink, urllib_transport
from tests.conftest import INFO, make_record

# -- 1. no clear-text key ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://specivo.example.com",
        "http://localhost:8030",
        "http://LOCALHOST",
        "http://127.0.0.1:8030/",
        "http://127.10.20.30",
        "http://[::1]:8030",
    ],
)
def test_https_or_this_machine_is_accepted(url: str) -> None:
    assert check_server_url(url) == url
    assert parse_config({"url": url}).url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://specivo.example.com",
        "http://10.0.0.5:8030",
        "http://localhost.example.com",
        "http://[::2]",
        "ftp://specivo.example.com",
        "https://user:secret@specivo.example.com",
        "https://",
    ],
)
def test_other_urls_are_refused(url: str) -> None:
    with pytest.raises(ConfigError):
        check_server_url(url)
    with pytest.raises(ConfigError):
        parse_config({"url": url})


def test_plain_http_on_a_private_network_needs_the_explicit_opt_in() -> None:
    url = "http://10.0.0.5:8030"
    assert parse_config({"url": url, "allow_insecure_http": True}).url == url
    with pytest.raises(ConfigError):
        parse_config({"url": url, "allow_insecure_http": "yes"})
    assert parse_config({}).allow_insecure_http is False
    text = render_template(url, allow_insecure_http=True)
    assert parse_config(tomllib.loads(text)).allow_insecure_http is True
    with pytest.raises(ConfigError):
        parse_config(tomllib.loads(render_template(url)))


def test_the_sink_refuses_plain_http_too() -> None:
    with pytest.raises(SinkError, match="https"):
        SpecivoSink("http://specivo.example.com", "spv_x")
    SpecivoSink("http://10.0.0.5", "spv_x", allow_insecure_http=True)


def test_init_refuses_plain_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO("spv_x\n"))
    args = ["init", "--url", "http://specivo.example.com", "--key-stdin", "--no-keyring", "--offline"]
    assert cli.main(args) == 2
    assert not (tmp_path / "cfg").exists()
    assert cli.main([*args, "--allow-insecure-http"]) == 0
    config_text = (tmp_path / "cfg" / "config.toml").read_text(encoding="utf-8")
    assert parse_config(tomllib.loads(config_text)).allow_insecure_http is True


# -- 2. redirects are never followed ---------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict[str, str]]

    def _handle(self) -> None:
        self.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps(INFO).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def other_host() -> Iterator[tuple[ThreadingHTTPServer, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []
    handler = type("Target", (_Recorder,), {"seen": seen})
    yield from ((httpd, seen) for httpd in _serve(handler))


def _redirector(target: str, redirect_get: bool) -> type[BaseHTTPRequestHandler]:
    class Redirector(BaseHTTPRequestHandler):
        def _redirect(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if redirect_get:
                self._redirect()
                return
            body = json.dumps(INFO).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self._redirect()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Redirector


@pytest.mark.parametrize("redirect_get", [True, False])
def test_a_redirect_to_another_host_never_gets_the_key(
    other_host: tuple[ThreadingHTTPServer, list[dict[str, str]]], redirect_get: bool
) -> None:
    target_server, seen = other_host
    # "localhost" is another host name than the 127.0.0.1 the sink was given.
    target = f"http://localhost:{target_server.server_address[1]}/api/v1/ai-usage/ingest"
    for redirector in _serve(_redirector(target, redirect_get)):
        url = f"http://127.0.0.1:{redirector.server_address[1]}"
        # The test means something: plain urllib would follow and hand the key over.
        request = urllib.request.Request(url, data=b"{}", headers={"Authorization": "Bearer spv_probe"})
        with urllib.request.urlopen(request, timeout=5):
            pass
        assert seen and seen[-1].get("authorization") == "Bearer spv_probe"
        seen.clear()

        assert urllib_transport("POST", url, {}, b"{}").status == 302
        sink = SpecivoSink(url, "spv_must_not_leak")
        with pytest.raises(SinkError, match=r"redirected \(302\) to http://localhost"):
            sink.send([make_record().to_wire()])
    assert seen == []


# -- 3. git never runs repository programs ----------------------------------------------


def test_every_git_call_carries_the_safety_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ai_usage_reporter.enrich.shutil.which", lambda name: "/usr/bin/git")
    monkeypatch.setattr("ai_usage_reporter.enrich.subprocess.run", fake_run)
    from datetime import UTC, datetime

    info = GitInfo(runner=run_git)
    info._emails[str(tmp_path)] = "dev@example.com"
    info.commits(str(tmp_path), datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 5, 1, tzinfo=UTC))
    [(cmd, env)] = calls
    assert cmd[1 : 1 + len(GIT_SAFETY_OPTIONS)] == list(GIT_SAFETY_OPTIONS)
    assert "--no-pager" in cmd and "core.fsmonitor=" in cmd and "gpg.program=false" in cmd
    assert cmd[cmd.index("log") + 1] == "--no-show-signature"
    for key, value in GIT_SAFETY_ENV.items():
        assert env[key] == value


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX owners")
def test_git_does_not_run_in_directories_of_other_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ai_usage_reporter.enrich.os.getuid", lambda: os.stat(tmp_path).st_uid + 1)
    monkeypatch.setattr("ai_usage_reporter.enrich.subprocess.run", lambda *a, **k: pytest.fail("git must not run"))
    assert run_git(["status"], str(tmp_path)) is None


def test_git_enrichment_can_be_switched_off() -> None:
    import argparse

    assert parse_config({"git_enrichment": False}).git_enrichment is False
    assert cli._git(argparse.Namespace(no_git=False), Config(git_enrichment=False)) is None
    assert cli._git(argparse.Namespace(no_git=True), Config()) is None
    assert cli._git(argparse.Namespace(no_git=False), Config()) is not None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.mark.skipif(shutil.which("git") is None or sys.platform.startswith("win"), reason="needs git and sh")
def test_a_hostile_repository_config_runs_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "marker"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 1\n', encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/app.git")
    _git(repo, "config", "core.fsmonitor", str(script))
    _git(repo, "config", "core.pager", f"{script} pager")
    _git(repo, "config", "diff.external", str(script))
    _git(repo, "config", "gpg.program", str(script))
    _git(repo, "config", "log.showSignature", "true")

    # The test means something: an ordinary git call in this repository runs the script.
    _git(repo, "status")
    if not marker.exists():
        pytest.skip("this git does not run core.fsmonitor programs")
    marker.unlink()

    for args in (["status"], ["log", "-1", "--format=%H"], ["diff", "HEAD"]):
        run_git(args, str(repo))
    from datetime import UTC, datetime, timedelta

    info = GitInfo()
    now = datetime.now(UTC)
    assert info.repo_hint(str(repo)) == "github.com/acme/app"
    assert len(info.commits(str(repo), now - timedelta(hours=1), now)) == 1
    assert not marker.exists()
