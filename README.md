# AI usage reporter for Specivo

`ai-usage` reads the logs that AI coding tools keep on your machine and reports their token usage
to [Specivo](https://specivo.io): tokens by class, requests, the model, the session and hints about
the issue the work was for. Specivo prices the usage, shows it next to the time logged on issues
and adds it to the project's spend reports.

- Reads **Claude Code** and **Codex CLI** logs. Other tools can be added as plugins.
- Speaks the Specivo AI usage ingest contract `specivo.ai-usage.v1` (Specivo 0.13.0 or later).
- Python 3.11 or later, standard library only. macOS, Linux and Windows.
- Sends counts and ids only. Prompts, code, tool output and file paths never leave your machine
  (see [Privacy](#privacy)).

Full documentation: <https://specivo.io/docs/ai-usage-reporter/> (the Markdown sources are in [`docs/`](docs/)).

Claude Code is a product of Anthropic and Codex of OpenAI. This tool is not affiliated with or
endorsed by either; their names only say which logs it reads.

## Install

From a release (there is no PyPI package):

```bash
uv tool install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z
# or: pipx install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z
```

Two optional extras:

| Extra | Adds |
|---|---|
| `keyring` | Keeps the API key in the OS keychain (macOS Keychain, Windows Credential Locker, Secret Service) |
| `zstd` | Reads compressed Codex rollouts (`*.jsonl.zst`) on Python versions before 3.14 |

```bash
uv tool install "specivo-ai-usage-reporter[keyring,zstd] @ git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z"
```

## Use

1. Create an API key in Specivo under **Your profile → API keys**.
2. Set up the reporter. It writes `config.toml` and stores the key in the keychain, or in a file
   only you can read:

   ```bash
   ai-usage init --url https://specivo.example.com
   ```

3. Look at what would be sent, then send it:

   ```bash
   ai-usage show-payload          # the exact JSON, nothing is sent
   ai-usage push                  # send new and changed usage
   ai-usage status                # what was sent, what waits for a retry, what was refused
   ai-usage doctor                # sources found, malformed lines, the server, last results
   ```

`push` options:

| Option | Meaning |
|---|---|
| `--source claude-code` | Only this source (repeatable) |
| `--session ID` | Only this session. With `SPECIVO_ISSUE=ACME-12` set, the session is attributed to that issue, now and in later pushes |
| `--since 2026-10-01` | Only UTC days on or after this date |
| `--dry-run` | Print the records instead of sending them; nothing is written |
| `--sink json --output usage.json` | Write the batch to a file instead of sending it |
| `--retry-rejected` | Send records again that the server refused before |
| `--no-git` | Do not run `git` for repository and commit hints |

Run `ai-usage push` after work, or on a timer (every 30 minutes is plenty). Sending is
idempotent: a bucket is sent again only when its totals or hints changed, and the server replaces
the totals of a bucket instead of adding to them. Session hooks for Claude Code and Codex come in
a later release; until then, use cron, a systemd user timer, launchd or Task Scheduler. For
example, with cron:

```cron
*/30 * * * * ai-usage push >/dev/null 2>&1
```

## What is sent

One record per **bucket**: one session of one tool, with one model, on one **UTC** day. A session
that runs past midnight UTC, or switches models, becomes several records. Each record carries:

- token counts by class (`input` without cache, `output` without reasoning, `cache_write_5m`,
  `cache_write_1h`, `cache_read`, `reasoning`) and the number of requests;
- the tool and its version, the provider and the model;
- the tool's session id (an opaque id; anything else is replaced by a hash);
- the first and last request of the bucket, as UTC timestamps;
- attribution hints: the issue from `SPECIVO_ISSUE`, the git branch, the repository as
  `host/owner/repo` (never a path, never credentials), SHAs of commits you made during the session,
  and a project key from your repository mapping;
- cost hints from your config (`cost_type`, `subscription_hint`) and `on_behalf_of` for service
  accounts.

Subagent work (Claude Code's Task tool and other sidechains) is counted in its parent session, so
a session's total includes what it fanned out.

## Privacy

- **Never sent, never printed, never logged:** prompts, responses, code, tool input and output,
  file paths, working directories, file names, git remote credentials.
- The record that goes on the wire is a separate type with exactly the contract's fields. It is
  built from an allowlist, field by field, and every value is checked against the contract's
  format; anything that does not fit is left out.
- `ai-usage show-payload` and `ai-usage push --dry-run` show exactly what would be sent.
- The API key only travels over `https://` (plain `http://` only to this machine, unless you set
  `allow_insecure_http = true`), and redirects are never followed.
- `git` (for repository and commit hints) runs read-only, only in directories you own, with the
  programs a repository can configure (fsmonitor, hooks, GPG, external diff, pager) switched off.
  `git_enrichment = false` or `push --no-git` turns it off.
- The local state database (in your user state directory) keeps file paths, to know which logs
  changed, and the records it sent. It never leaves your machine.
- The test suite plants canary strings in prompts, tool output, paths and git credentials of the
  fixtures and checks that none of them reaches any sink, the console or the state ledger.

## Configuration

`config.toml` lives in the user config directory (`~/Library/Application Support/ai-usage-reporter`
on macOS, `%APPDATA%\ai-usage-reporter` on Windows, `~/.config/ai-usage-reporter` elsewhere). Every
setting is described in [docs/index.md](docs/index.md#configuration).

```toml
url = "https://specivo.example.com"
timezone = "Asia/Bangkok"          # for dates shown by "status"; buckets are always UTC days
git_commits = true
exclude_paths = ["~/private"]

[[cost_rules]]
tool = "claude-code"
cost_type = "subscription"
subscription_hint = "claude-max-20x"

[projects]
"github.com/acme/*" = "ACME"
```

## Writing an adapter

An adapter reads one tool's logs. It implements a small `Protocol` and registers through the
entry-point group `ai_usage_reporter.adapters`; the conformance kit `ai_usage_reporter.testing`
checks it. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
uv sync
make test        # pytest
make lint        # ruff and mypy --strict
```

`tests/test_e2e.py` sends records to a real Specivo server. It runs only when
`SPECIVO_E2E_URL` and `SPECIVO_E2E_API_KEY` point at a disposable test instance.

## License

MIT, see [LICENSE](LICENSE).
