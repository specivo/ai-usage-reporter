"""Turning buckets into contract records: attribution hints, cost hints, privacy.

For each bucket:

- ``issue_ref``: the ``SPECIVO_ISSUE`` captured for the session (sent with
  ``issue_ref_source: "env"``).
- ``git_branch``: the branch the tool logged.
- ``repo``: the tool's logged repository URL, else the ``origin`` remote of the
  bucket's working directory, as ``host/owner/repo``. Never a path.
- ``commits``: SHAs of commits the local git user made in that repository
  during the bucket's time window (``git log``), when ``git_commits`` is on.
- ``project_key``: from the ``[projects]`` repository mapping.
- ``cost_type`` and ``subscription_hint``: from the first matching
  ``[[cost_rules]]`` entry.

The working directory is read here, locally, and never copied into a record.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ai_usage_reporter.aggregate import Bucket
from ai_usage_reporter.config import Config
from ai_usage_reporter.record import (
    Attribution,
    ContractRecord,
    idempotency_key,
    normalise_repo,
    opaque_session_id,
)

GitRunner = Callable[[Sequence[str], str], str | None]

#: Commits shortly after the last request still belong to the session.
COMMIT_GRACE = timedelta(minutes=10)
GIT_TIMEOUT_SECONDS = 5.0


def run_git(args: Sequence[str], cwd: str) -> str | None:
    """Run ``git -C <cwd> <args>``; ``None`` on any failure."""
    git = shutil.which("git")
    if git is None or not Path(cwd).is_dir():
        return None
    try:
        result = subprocess.run(
            [git, "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


@dataclass
class GitInfo:
    """Cached git lookups per working directory."""

    runner: GitRunner = run_git
    _remotes: dict[str, str | None] = field(default_factory=dict)
    _emails: dict[str, str | None] = field(default_factory=dict)

    def repo_hint(self, cwd: str) -> str | None:
        if cwd not in self._remotes:
            out = self.runner(["remote", "get-url", "origin"], cwd)
            self._remotes[cwd] = normalise_repo(out.strip()) if out else None
        return self._remotes[cwd]

    def user_email(self, cwd: str) -> str | None:
        if cwd not in self._emails:
            out = self.runner(["config", "user.email"], cwd)
            self._emails[cwd] = out.strip() if out and out.strip() else None
        return self._emails[cwd]

    def commits(self, cwd: str, start: datetime, end: datetime) -> tuple[str, ...]:
        email = self.user_email(cwd)
        if email is None:
            return ()
        out = self.runner(
            [
                "log",
                "--all",
                f"--since={start.isoformat()}",
                f"--until={(end + COMMIT_GRACE).isoformat()}",
                f"--author=<{email}>",
                "--fixed-strings",
                "--format=%H",
                "-n",
                "100",
            ],
            cwd,
        )
        if not out:
            return ()
        return tuple(sorted({line.strip() for line in out.splitlines() if line.strip()}))


@dataclass
class Enricher:
    config: Config
    session_issues: Mapping[str, str] = field(default_factory=dict)
    git: GitInfo | None = None
    drop_explicit: Callable[[str], bool] = lambda key: False

    def record(self, bucket: Bucket) -> ContractRecord:
        session = opaque_session_id(bucket.session_id)
        key = idempotency_key(bucket.tool, session, bucket.model, bucket.usage_date)
        cwd = bucket.cwd
        repo = normalise_repo(bucket.repo_url)
        commits: tuple[str, ...] = ()
        if self.git is not None and cwd:
            if repo is None:
                repo = self.git.repo_hint(cwd)
            if self.config.git_commits:
                commits = self.git.commits(cwd, bucket.started_at, bucket.ended_at)
        issue = self.session_issues.get(bucket.session_id)
        attribution = Attribution(
            issue_ref=issue,
            issue_ref_source="env" if issue else None,
            project_key=self.config.project_for(repo),
            git_branch=bucket.git_branch,
            repo=repo,
            commits=commits,
        )
        if self.drop_explicit(key):
            attribution = attribution.without_explicit()
        rule = self.config.cost_for(bucket.tool, bucket.model, repo)
        return ContractRecord(
            idempotency_key=key,
            tool=bucket.tool,
            model=bucket.model,
            usage_date=bucket.usage_date,
            tokens=bucket.tokens,
            long_context_tokens=None if bucket.long_tokens.is_zero() else bucket.long_tokens,
            tool_version=bucket.tool_version,
            provider=bucket.provider,
            session_id=session,
            started_at=bucket.started_at,
            ended_at=bucket.ended_at,
            request_count=bucket.request_count,
            cost_type=rule.cost_type if rule else None,
            subscription_hint=rule.subscription_hint if rule else None,
            on_behalf_of=self.config.on_behalf_of,
            attribution=attribution,
        )
