---
description: Report the token usage of Claude Code and Codex to Specivo with the open-source ai-usage reporter. Counts and ids only; prompts, code and paths stay on your machine.
---

# AI usage reporter

`ai-usage` is a small command-line tool that reads the logs AI coding tools keep on your machine
and reports their token usage to Specivo. Specivo prices the usage with its own price table, shows
it next to the time logged on issues, and adds it to the project's AI spend reports.

The tool is open source (MIT) at
[github.com/specivo/ai-usage-reporter](https://github.com/specivo/ai-usage-reporter). It speaks
the AI usage ingest contract `specivo.ai-usage.v1`, so it needs Specivo 0.13.0 or later.

!!! note "Why a reporter?"
    An agent connected to Specivo over MCP cannot see its own token counters. The tools write them
    to local logs, and the reporter reads them from there after the work is done.

## What it reads

| Tool | Logs | Notes |
|---|---|---|
| Claude Code (`claude-code`) | `~/.claude/projects/` and `~/.config/claude/projects/`; `CLAUDE_CONFIG_DIR` moves them | Subagent work counts in the parent session; repeated copies of a response count once |
| Codex CLI (`codex`) | `~/.codex/sessions/` and `~/.codex/archived_sessions/`; `CODEX_HOME` moves them | Active rollouts win over archived copies; compressed rollouts need the `zstd` extra before Python 3.14 |

More tools can be added as plugins (see [Writing an adapter](#writing-an-adapter)).

## Install

You need Python 3.11 or later, or [uv](https://docs.astral.sh/uv/), which installs Python for you.
There is no PyPI package; install a release tag from GitHub:

```bash
uv tool install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z
# or
pipx install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z
```

With the optional extras (the OS keychain for the key, compressed Codex logs):

```bash
uv tool install "specivo-ai-usage-reporter[keyring,zstd] @ git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z"
```

## Set up

1. In Specivo, open **Your profile → API keys** and create a key. The account that owns it is the
   reporter of everything it sends. It needs **Log AI usage** in the projects your usage is
   attributed to; usage without a project needs no permission.
2. Run:

   ```bash
   ai-usage init --url https://your-specivo-host
   ```

   It writes `config.toml`, asks for the key without showing it, stores it in the OS keychain (or,
   without the `keyring` extra, in an `api-key` file only you can read) and checks the server.
   The key can also come from the `SPECIVO_API_KEY` environment variable.

3. Check the setup:

   ```bash
   ai-usage doctor
   ```

   It lists the log directories found, how many files and requests it read, malformed lines, the
   server's contract version and clock, and the results of the last push.

## Send usage

```bash
ai-usage show-payload     # print exactly what would be sent; nothing is sent
ai-usage push             # send new and changed usage
ai-usage status           # what was sent, what waits for a retry, what was refused
```

`push` only sends buckets whose totals or hints changed since the last push, so it is cheap to run
often. After a crash or a lost connection, just run it again: Specivo replaces a bucket's totals,
it never adds them up.

| Option | Meaning |
|---|---|
| `--source NAME` | Only `claude-code` or `codex` (repeatable) |
| `--session ID` | Only this session |
| `--since YYYY-MM-DD` | Only UTC days on or after this date |
| `--dry-run` | Print the records instead of sending them; nothing is written locally either |
| `--sink json --output FILE` | Write the batch to a file |
| `--retry-rejected` | Send records again that the server refused before |
| `--no-git` | Do not ask `git` for repository and commit hints |

### On a timer

Session hooks for Claude Code and Codex are planned. Until then, run `push` on a timer, for
example every 30 minutes:

=== "cron (Linux, macOS)"

    ```cron
    */30 * * * * ai-usage push >/dev/null 2>&1
    ```

=== "Task Scheduler (Windows)"

    ```powershell
    schtasks /Create /SC MINUTE /MO 30 /TN "ai-usage push" /TR "ai-usage push"
    ```

## Which issue the usage counts for

Each record carries hints; Specivo decides (see
[Reporting AI usage](../specivo/ai-agents/reporting-ai-usage.md#which-issue-the-usage-counts-for)):

1. **`SPECIVO_ISSUE`**. Set it when you push one session, and that session is reported for the
   issue, now and on every later push:

   ```bash
   SPECIVO_ISSUE=ACME-12 ai-usage push --session 0f6c2a1e-...
   ```

2. Specivo's own record of an agent working on an issue over MCP during the session.
3. **Commits** you made in the session's repository while it ran (found with `git log`, by your
   `git config user.email`).
4. **The branch**, when its name holds an issue key such as `feat/acme-12-login`.
5. **The repository**: a `[projects]` mapping in your config names the project; otherwise
   Specivo matches `host/owner/repo` to a connected repository.

Once an entry is attributed in Specivo, later pushes keep that attribution. If a push names a
different issue or project, Specivo refuses it with `attribution_conflict`. The reporter then
sends the bucket once more without the issue and project it named, so the totals stay current, and
lists the bucket in `ai-usage status`. Move the entry in Specivo if the old attribution is wrong.

## Days are UTC

A record is one **bucket**: one session of one tool, with one model, on one **UTC** day. A session
that runs past midnight UTC (07:00 in Bangkok, for example) becomes two records. This keeps
Specivo's price and exchange-rate lookups the same for everyone. Specivo shows days in each
viewer's own time zone; the `timezone` setting only changes how `ai-usage status` shows times.

## Costs

The reporter sends tokens; Specivo computes the cost. Tell it how the usage is paid with
`[[cost_rules]]` in the config (per tool, model or repository):

```toml
[[cost_rules]]
tool = "claude-code"
cost_type = "subscription"
subscription_hint = "claude-max-20x"

[[cost_rules]]
repo = "github.com/acme/*"
cost_type = "api"
```

Without a rule, Specivo uses `subscription` when you have an AI subscription covering the tool,
and `unknown` otherwise.

### Long context

Some models bill a whole request at higher rates once its context (input plus cache reads and
writes) passes a threshold. The reporter cannot see Specivo's price table, so list such models in
`[long_context]`:

```toml
[long_context]
"example-model-*" = 200000
```

A request above the threshold is then reported in `long_context_tokens` instead of `tokens`.

## Reporting for a teammate

A service account that runs the reporter for one person sets `on_behalf_of = "login"` in the
config. The account needs **Log AI usage for others** in the project, and the person must be an
active member. So that the field cannot be used to find out which logins exist, Specivo answers
`forbidden` for every refusal unless the record names a project the account may report for; map
your repositories in `[projects]` to get the precise reasons.

## Privacy

- **Sent:** token counts, request counts, the tool, its version, the provider and model, the
  tool's session id, the bucket's UTC day and first and last request time, the git branch, the
  repository as `host/owner/repo`, commit SHAs, the issue from `SPECIVO_ISSUE`, the project key
  from your mapping, cost hints and `on_behalf_of`.
- **Never sent, printed or logged:** prompts, responses, code, tool input and output, file paths,
  working directories, file names, remote URLs with credentials.
- The record sent is built from an allowlist and checked field by field against the contract.
- `ai-usage show-payload` and `push --dry-run` show exactly what would be sent.
- The local state database keeps the paths of the log files it read, to skip unchanged ones, and
  the records it sent. It stays on your machine.
- `exclude_paths` in the config keeps sessions that ran in those directories out entirely.

## Configuration

`config.toml` is in the user config directory:

| System | Config | State |
|---|---|---|
| macOS | `~/Library/Application Support/ai-usage-reporter/` | the same |
| Linux | `~/.config/ai-usage-reporter/` | `~/.local/state/ai-usage-reporter/` |
| Windows | `%APPDATA%\ai-usage-reporter\` | `%LOCALAPPDATA%\ai-usage-reporter\` |

`AI_USAGE_CONFIG_DIR` and `AI_USAGE_STATE_DIR` move them. Every setting:

| Setting | Meaning |
|---|---|
| `url` | The Specivo server |
| `timezone` | Time zone for times shown by `status` (buckets are always UTC days) |
| `on_behalf_of` | The login usage is reported for (service accounts) |
| `git_commits` | Add SHAs of your commits made during the session (default `true`) |
| `exclude_paths` | Sessions that ran in these directories are never reported |
| `[sources.<name>]` | `enabled = false` turns a source off; `roots = [...]` reads other directories |
| `[[cost_rules]]` | `tool`, `model`, `repo` (patterns) → `cost_type`, `subscription_hint`; the first match wins |
| `[projects]` | Repository pattern (`host/owner/repo`) → Specivo project key |
| `[long_context]` | Model pattern → long-context threshold in tokens |

## Troubleshooting

| Symptom | What to do |
|---|---|
| `no API key` | Run `ai-usage init`, or set `SPECIVO_API_KEY` |
| `the api-key file can be read by other users` | `chmod 600` the file in the config directory |
| `does not accept specivo.ai-usage.v1` | Upgrade Specivo to 0.13.0 or later |
| `doctor` warns about the clock | Fix the computer's clock; records dated in the future are refused |
| Records refused with `forbidden` | The account lacks **Log AI usage** in the project, or (with `on_behalf_of`) **Log AI usage for others** |
| Records refused with `key_conflict` | Another account already reported these buckets for you; use one reporter per person |
| `push` exits with status 1 | Some records are waiting for a retry (server busy or offline); the next push sends them |
| Codex rollouts are skipped | Install the `zstd` extra |

## Writing an adapter

The reporter reads other tools through plugins. A plugin package implements the `Adapter`
protocol, registers it under the entry-point group `ai_usage_reporter.adapters` and passes the
conformance kit in `ai_usage_reporter.testing`. The checklist is in
[CONTRIBUTING.md](https://github.com/specivo/ai-usage-reporter/blob/main/CONTRIBUTING.md).
