---
description: Report the token usage of Claude Code, Codex, Gemini CLI, OpenCode and the tools ccusage reads to Specivo with the open-source ai-usage reporter. Counts and ids only; prompts, code and paths stay on your machine.
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
| Gemini CLI (`gemini-cli`) | `~/.gemini/tmp/<project>/chats/` (and `~/.cache/.gemini` under the macOS sandbox), JSON and JSONL recordings | Cached prompt tokens are reported as cache reads, thoughts as reasoning; subagents count in their parent session |
| OpenCode (`opencode`) | `opencode.db` in `~/.local/share/opencode/` (`XDG_DATA_HOME` and `OPENCODE_DB` move it), and the older `storage/` JSON files | Read-only, never locked; child sessions count in their root session; OpenCode's cost is sent as the reported cost |
| Others, through [ccusage](https://ccusage.com) | the JSON of `ccusage daily --by-agent --json` | Day totals per tool and model; see [Import ccusage](#import-ccusage) |

More tools can be added as plugins (see [Writing an adapter](#writing-an-adapter)).

### How the token classes are read

Specivo counts every token in exactly one class: `input` without the cache, `output` without
reasoning, cache writes, cache reads and reasoning.

- **Claude Code** logs them that way already.
- **Codex** counts cached input inside its input and reasoning inside its output; both are moved
  to their own classes.
- **Gemini CLI** counts the cached part of the prompt inside `input` (it is moved to cache reads),
  logs `thoughts` apart from the output (reported as reasoning), and logs the prompt of tool-use
  calls apart from `input` (it is added to input).
- **OpenCode** stores disjoint classes already: `input` without cache reads and writes, `output`
  without `reasoning`. Its single cache-write count is reported as 5-minute cache writes.
- **ccusage** reports input without the cache; its cache creation is reported as 5-minute cache
  writes. It has no reasoning class and no request counts.

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
| `--source NAME` | Only `claude-code`, `codex`, `gemini-cli` or `opencode` (repeatable) |
| `--session ID` | Only this session (a subagent's session id selects its parent session) |
| `--since YYYY-MM-DD` | Only UTC days on or after this date |
| `--dry-run` | Print the records instead of sending them; nothing is written locally either |
| `--sink json --output FILE` | Write the batch to a file |
| `--retry-rejected` | Send records again that the server refused before |
| `--no-git` | Do not ask `git` for repository and commit hints |

### From the tools' hooks

`ai-usage hooks install --tool <tool>` makes a tool push each session itself (`--tool all` for
every one, `uninstall` to take it out again):

| Tool | Installed into | Runs |
|---|---|---|
| Claude Code | `settings.json` in `~/.claude` (or `CLAUDE_CONFIG_DIR`): a command hook on `SessionEnd` and `Stop` | `ai-usage hook claude-code`, which reads `session_id` from the hook's input |
| Codex CLI | `config.toml` in `~/.codex` (or `CODEX_HOME`): the `notify` program, on `agent-turn-complete` | `ai-usage hook codex`, which reads `thread-id` from the notification |
| Gemini CLI | `~/.gemini/settings.json`: a command hook on `SessionEnd` and `AfterAgent` | `ai-usage hook gemini-cli`, which reads `session_id` from the hook's input |
| OpenCode | `~/.config/opencode/plugins/specivo-ai-usage-reporter.js` (`XDG_CONFIG_HOME` moves it) | `ai-usage push --source opencode --session <id>` when a session goes idle |

How they behave:

- **Fast and fail-open.** The hook starts the push in the background and returns at once, always
  with exit code 0, so it never blocks or slows the tool. The push writes a short summary (counts
  and statuses, no content) to `hooks.log` in the state directory.
- **Debounced.** Turn events come often. A push waits 30 seconds (20 in OpenCode) and runs only if
  no later turn of the same session came in meanwhile, so a burst of turns makes one push. A
  session end pushes at once.
- **Safe edits.** Only the reporter's own entry is added or removed; your other hooks and settings
  stay as they are. A file is copied to `<name>.ai-usage-backup-<time>` before it changes (for
  OpenCode, into `~/.config/opencode/ai-usage-backups/`), and installing twice changes nothing.
  A settings file with comments is left alone with a message.
- **Codex has one `notify` program.** If you have one, the reporter's hook runs it too, with the
  same notification, and `uninstall` puts yours back.
- **`SPECIVO_ISSUE`** set where you start the tool reaches the push, and the session is attributed
  to that issue. Gemini CLI runs hooks with a reduced environment, so there it may not arrive.
- Gemini CLI's `/hooks` command lists the installed hook and can switch it off.
- The hooks name the reporter by its full path. After reinstalling the reporter somewhere else,
  run `ai-usage hooks install` again.

### On a timer

Hooks miss sessions that never end cleanly, so also push on a timer:

```bash
ai-usage schedule install           # every 30 minutes
ai-usage schedule install --every 15
ai-usage schedule remove
```

| System | What is set up |
|---|---|
| macOS | a launchd agent `~/Library/LaunchAgents/io.specivo.ai-usage-reporter.plist`, loaded with `launchctl bootstrap` |
| Linux | a systemd user timer `ai-usage-reporter.timer` (and `.service`) in `~/.config/systemd/user`, enabled with `systemctl --user enable --now` |
| Windows | a Task Scheduler task `SpecivoAIUsageReporter` (`schtasks /Create`) |

`--kind launchd|systemd|schtasks` picks another one. The push writes to `schedule.log` in the state
directory. A systemd user timer only runs while you are logged in, unless lingering is on
(`loginctl enable-linger`).

With cron instead:

```cron
*/30 * * * * /full/path/to/ai-usage push >/dev/null 2>&1
```

cron starts with a short `PATH`, so use the full path of `ai-usage` (`which ai-usage`).

#### Checking a schedule by hand

The installers are tested with the system commands recorded, not run. To check one on a real
machine:

1. `ai-usage schedule install --every 5`, then wait five minutes.
2. `ai-usage status` shows a new "last push"; `schedule.log` in the state directory has its output.
3. macOS: `launchctl print gui/$(id -u)/io.specivo.ai-usage-reporter` shows the agent.
   Linux: `systemctl --user list-timers ai-usage-reporter.timer` shows the next run.
   Windows: `schtasks /Query /TN SpecivoAIUsageReporter` shows the task.
4. `ai-usage schedule remove`; the same commands then find nothing.

## Import ccusage

[ccusage](https://ccusage.com) reads the logs of many AI tools. `ai-usage import ccusage` sends its
daily report, so those tools are covered at day granularity:

```bash
ccusage daily --by-agent --json > usage.json
ai-usage import ccusage usage.json --dry-run    # look first
ai-usage import ccusage usage.json
# or: ccusage daily --by-agent --json | ai-usage import ccusage
```

- One record per tool, model and day, without a session or request count. ccusage's agent names
  become tool slugs (`claude` is `claude-code`, `gemini` is `gemini-cli`, others as they are).
  Rows without an agent count for `--default-agent` (`claude`).
- Importing the same days again replaces their totals.
- ccusage's own cost estimate is not sent; Specivo prices the tokens.
- Buckets are UTC days. Run ccusage in UTC (its `--timezone UTC` option, where your version has
  it), or days near midnight land on your local date.
- **One source per tool.** A tool is counted either from its own logs or from the import, never
  both. Rows of tools the reporter reads itself are skipped (the import says how many). To count a
  tool through ccusage instead, set `via = "ccusage"`:

  ```toml
  [sources.gemini-cli]
  via = "ccusage"
  ```

  `push` then leaves that tool's logs alone. Records already sent by the other route stay in
  Specivo under their own keys; delete them there if you switch a tool over.

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

When a tool logs what each request cost (OpenCode does, in US dollars), the bucket's sum is sent
as `reported_cost`, but only when every request of the bucket has a cost and the sum is above
zero. Specivo uses it as the spend of `api` usage and keeps it for the others.

### Long context

Some models bill a whole request at higher rates once its context (input plus cache reads and
writes) passes a threshold. The reporter cannot see Specivo's price table, so list such models in
`[long_context]`:

```toml
[long_context]
"example-model-*" = 200000
```

A request above the threshold is then reported in `long_context_tokens` instead of `tokens`. The
two never overlap: each request's tokens are in exactly one of them, and their sum is the bucket's
usage.

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
- The API key is only sent over `https://` (or to this machine), and redirects are never
  followed, so the key cannot be forwarded to another address. A server that redirects makes
  `push` stop with an error naming the target; put the final address in `url`.
- `git` runs read-only, only in directories you own, with git's own `safe.directory` check on,
  and with every program a repository can configure (fsmonitor, hooks, GPG, external diff,
  pager) switched off on the command line.

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
| `url` | The Specivo server. Must be `https://`; plain `http://` is accepted only for this machine (`localhost`, `127.0.0.0/8`, `::1`) |
| `allow_insecure_http` | `true` also accepts plain `http://` to another machine. The API key then travels unencrypted, so use it only for a test server on a network you trust. Default `false`; `ai-usage init --allow-insecure-http` sets it |
| `git_enrichment` | Ask `git` for the repository and commit hints (default `true`); `false` never runs git, like `push --no-git` |
| `timezone` | Time zone for times shown by `status` (buckets are always UTC days) |
| `on_behalf_of` | The login usage is reported for (service accounts) |
| `git_commits` | Add SHAs of your commits made during the session (default `true`; needs `git_enrichment`) |
| `exclude_paths` | Sessions that ran in these directories are never reported |
| `[sources.<name>]` | `enabled = false` turns a source off; `roots = [...]` reads other directories; `via = "ccusage"` counts the tool through `import ccusage` instead of its logs |
| `[[cost_rules]]` | `tool`, `model`, `repo` (patterns) → `cost_type`, `subscription_hint`; the first match wins |
| `[projects]` | Repository pattern (`host/owner/repo`) → Specivo project key |
| `[long_context]` | Model pattern → long-context threshold in tokens |

## Troubleshooting

| Symptom | What to do |
|---|---|
| `no API key` | Run `ai-usage init`, or set `SPECIVO_API_KEY` |
| `the api-key file can be read by other users` | `chmod 600` the file in the config directory |
| `does not accept specivo.ai-usage.v1` | Upgrade Specivo to 0.13.0 or later |
| `the server redirected (30x) to ...` | Put the address the redirect names (usually the `https://` one) in `url` |
| `doctor` warns about the clock | Fix the computer's clock; records dated in the future are refused |
| Records refused with `forbidden` | The account lacks **Log AI usage** in the project, or (with `on_behalf_of`) **Log AI usage for others** |
| Records refused with `key_conflict` | Another account already reported these buckets for you; use one reporter per person |
| `push` exits with status 1 | Some records are waiting for a retry (server busy or offline); the next push sends them |
| Codex rollouts are skipped | Install the `zstd` extra |
| `doctor` says an OpenCode database is busy | OpenCode was writing; the next push reads it |
| A hook does not seem to run | Look at `hooks.log` in the state directory; run `ai-usage hooks install --tool <tool>` again after moving the reporter |

## Writing an adapter

The reporter reads other tools through plugins. A plugin package implements the `Adapter`
protocol, registers it under the entry-point group `ai_usage_reporter.adapters` and passes the
conformance kit in `ai_usage_reporter.testing`. The checklist is in
[CONTRIBUTING.md](https://github.com/specivo/ai-usage-reporter/blob/main/CONTRIBUTING.md).
