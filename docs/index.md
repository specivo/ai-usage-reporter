---
description: Report the token usage of Claude Code, Codex, Gemini CLI, OpenCode, Hermes Agent and the tools ccusage reads to Specivo with the open-source ai-usage reporter. Counts and ids only; prompts, code and paths stay on your machine.
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
| Codex CLI (`codex`) | `~/.codex/sessions/` and `~/.codex/archived_sessions/`; `CODEX_HOME` moves them | Active rollouts win over archived copies; compressed rollouts need the `zstd` extra before Python 3.14; a subagent's rollout counts in its parent session (the copy of the parent's history it starts with is skipped) |
| Gemini CLI (`gemini-cli`) | `~/.gemini/tmp/<project>/chats/` (and `~/.cache/.gemini` under the macOS sandbox), JSON and JSONL recordings | Cached prompt tokens are reported as cache reads, thoughts as reasoning; subagents count in their parent session |
| OpenCode (`opencode`) | `opencode.db` in `~/.local/share/opencode/` (`XDG_DATA_HOME` and `OPENCODE_DB` move it), and the older `storage/` JSON files | Read-only, never locked; child sessions count in their root session; OpenCode's cost is sent as the reported cost |
| Hermes Agent (`hermes`) | `state.db` in `~/.hermes/` and in each `~/.hermes/profiles/<name>/` (`HERMES_HOME` moves it) | Read-only; running totals read as growth since the last push; child sessions count in their root session; see [Hermes Agent](#hermes-agent) |
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
- **Hermes** stores `input` without cache reads and writes for every provider, and `output` with
  the reasoning it also reports apart: output is sent as `output - reasoning`. Its single
  cache-write count is reported as 5-minute cache writes.
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
ai-usage show-payload     # print exactly what a push would send now; nothing is sent (--all: every record)
ai-usage push             # send new and changed usage
ai-usage status           # what was sent, what waits for a retry, what was refused
```

`push` only sends buckets whose totals or hints changed since the last push, so it is cheap to run
often. After a crash or a lost connection, just run it again: Specivo replaces a bucket's totals,
it never adds them up.

| Option | Meaning |
|---|---|
| `--source NAME` | Only `claude-code`, `codex`, `gemini-cli`, `opencode` or `hermes` (repeatable) |
| `--session ID` | Only this session (a subagent's session id selects its parent session) |
| `--since YYYY-MM-DD` | Only UTC days on or after this date |
| `--dry-run` | Print the records a push would send (new or changed since the last push, then the queue) instead of sending them; nothing is written locally either. Add `--all` for every collected record |
| `--sink json --output FILE` | Write the batch to a file |
| `--retry-rejected` | Send records again that the server refused before |
| `--no-git` | Do not ask `git` for repository and commit hints |

### From the tools' hooks

`ai-usage hooks install --tool <tool>` makes a tool push each session itself (`--tool all` for
every one, `uninstall` to take it out again):

| Tool | Installed into | Runs |
|---|---|---|
| Claude Code | `settings.json` in `~/.claude` (or `CLAUDE_CONFIG_DIR`): a command hook on `SessionStart`, `SessionEnd` and `Stop` | `ai-usage hook claude-code`, which reads `session_id` from the hook's input; `SessionStart` only records the session's route (see [Route and provider](#route-and-provider)) |
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
- Claude Code hooks installed before the reporter knew `SessionStart` miss the route of new
  sessions until you run `ai-usage hooks install --tool claude-code` again.
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
the ingest contract bundled with the reporter,
[`contract/v1/README.md`](../src/ai_usage_reporter/contract/v1/README.md#issue-attribution)):

1. **`SPECIVO_ISSUE`**. Set it when you push one session, and that session is reported for the
   issue, now and on every later push:

   ```bash
   SPECIVO_ISSUE=ACME-12 ai-usage push --session 0f6c2a1e-...
   ```

2. **The issues the agent marked** with its own Specivo tool calls, read from the tool's
   transcript (see below).
3. Specivo's own record of an agent working on an issue over MCP during the session: first the
   agent session with the same session id, then, when there is none, **any** of your agent
   sessions that worked on an issue at the same time (see
   [Keep other work out of Specivo issues](#keep-other-work-out-of-specivo-issues)).
4. **Commits** you made in the session's repository while it ran (found with `git log`, by your
   `git config user.email`).
5. **The branch**, when its name holds an issue key such as `feat/acme-12-login`.
6. **The repository**: a `[projects]` mapping in your config names the project; otherwise
   Specivo matches `host/owner/repo` to a connected repository.

Once an entry is attributed in Specivo, later pushes keep that attribution. If a push names a
different issue or project, Specivo refuses it with `attribution_conflict`. The reporter then
sends the bucket once more without the issue and project it named, so the totals stay current, and
lists the bucket in `ai-usage status`. Move the entry in Specivo if the old attribution is wrong.

### Keep other work out of Specivo issues

The reporter reads every session of every tool on the machine, whatever folder it ran in. A
session that names no issue and no project of its own can be matched by time in step 3: if you
run Codex in a client's repository while a Claude Code session in another repository works on
`ACME-12`, the Codex usage may be counted for `ACME-12`.

Give every repository you work in a project, so this cannot happen:

```toml
[projects]
"github.com/acme/*" = "ACME"                  # your own product
"git.client-a.example/*" = "CLIENTA"          # everything on the client's git host
"github.com/client-b/billing-api" = "CLIENTB" # one repository
```

- A pattern is matched against `host/owner/repo` (lower case, without `https://`, `git@` or
  `.git`), and `*` also matches `/`, so `host/*` covers every group and subgroup on that host.
  The first pattern that matches wins, so put specific ones before broad ones.
- With a project key on the record, Specivo drops an inferred issue (steps 3 to 5) that belongs to
  another project and keeps the entry in the named project.
- Sessions outside any git repository have no `host/owner/repo`: set `project` under
  `[sources.<tool>]`, or set `SPECIVO_ISSUE` in that tool's environment.
- Work that should never reach Specivo goes into `exclude_paths` instead
  (`exclude_paths = ["~/code/client-a"]`).
- Check the result with `ai-usage show-payload`: each record shows the repository and the
  `project_key` it carries.

### Long sessions on several issues

People keep one agent session open for hours, and the agent works on one issue after another or
starts subagents for several at once. The agent says which issue it works on when it calls
Specivo's MCP tools, and the transcript keeps those calls. The reporter reads them and splits the
session into **issue segments**:

- **Marking calls**: `specivo_update_issue` that changes the status or the assignee,
  `specivo_timer` with `op = "start"`, and `specivo_create_issue` (the new key, from the tool's
  answer). Reading an issue, commenting or other calls mark nothing. The MCP server may have any
  name.
- Usage from one marking call to the next counts for that call's issue. Usage before the first
  marking call counts for the first marked issue: the research before the issue existed.
- A subagent counts for the issue it marked itself; one that marked none counts for the issue the
  session worked on when the subagent started. Parallel subagents on different issues each count
  for their own.
- A session that never marks an issue is reported as before.

Each segment becomes its own record (one session, model, UTC day and issue), sent with
`"issue_ref_source": "agent_log"`. Specivo treats it as a strong hint, not as your choice: it
replaces an issue found from commits or the branch and refines an entry that only has a project,
but never an issue you set with `SPECIVO_ISSUE` or moved by hand, and an issue it cannot use is
dropped with a warning instead of refusing the record. `SPECIVO_ISSUE` wins for the whole session.

The segment holding the day's first request keeps the record's usual key, so a bucket pushed before
the split is updated in place. When a later push moves usage to another segment, a key that no
longer has any is sent with zero tokens, so the day's total in Specivo stays right.

Only the tool name, the arguments that decide the mark (`issue_ref`, `status_id`,
`assigned_to_id`, `op`) and the issue key in `specivo_create_issue`'s answer are read. Nothing else
of those calls leaves your machine. `ai-usage show-payload` shows the issue of each record.

| Tool | Segments |
|---|---|
| Claude Code | Yes, main thread and subagents |
| Codex | Yes (`McpToolCall` items of `item_completed` events in current rollouts, also for calls made from code mode; `function_call` and `mcp_tool_call_end` in older ones). A subagent's rollout is a thread of its parent session |
| OpenCode 2 | Yes, for status, assignee and timer calls (also calls made from code); a created issue's key is only read from a direct call's answer. Child sessions are threads of their root session |
| Hermes | Yes, from the `tool_calls` of assistant messages (`mcp__<server>__specivo_*`); a created issue's key is read from the tool's answer. Child sessions are threads of their root session. Usage is placed at the time Hermes last saw it, so marks split it at push granularity |
| OpenCode 1, Gemini CLI, `import ccusage` | Not read (OpenCode 1 keeps tool calls in a table of their own, ccusage has day totals only); Specivo's own match of MCP sessions (step 3) still applies |

Set `agent_log_issues = false` to turn it off: sessions are then reported as one record per
model and day again (segments already pushed are sent with zero tokens).

A Specivo server older than this feature refuses `agent_log`. The reporter then sends those
records again without the issue, so the totals stay right, and leaves the issue out for the next
seven days before trying again.

## Active time

Agents are bad at guessing how long they worked. The transcripts already hold a timestamp for
every prompt, model answer and tool call, so the reporter measures the time instead and sends it
with each record: `active_seconds` (how long the agent was busy) and `active_spans` (when, as
start and end times to the second), with `active_time_version` naming the rules below.

How it is measured (definition version 1; the numbers are fixed, not settings):

- Each thread counts on its own: the main conversation and every subagent.
- A tool call counts from its start to its result, up to 60 minutes, so a six minute test run is
  six minutes of work although nothing is logged while it runs.
- Any other gap between two lines of the transcript counts when it is 10 minutes or shorter
  (the model thinking and writing); a longer one is a stall or a closed laptop and counts nothing.
- Time between turns never counts: waiting for you to answer, for a background job to finish or
  for a subagent to hand back. A prompt starts a turn, whether you typed it or it is such a
  notification.
- A question the agent asks you (Claude Code `AskUserQuestion` and `ExitPlanMode`, Codex
  `request_user_input`) counts nothing, however long you take. A call you approved or refused by hand counts only through the 10 minute rule, so the
  time the permission prompt stayed open is not counted. A parent's wait for a foreground subagent
  (Claude Code) or a collaboration `wait` (Codex) is not a tool run: the subagent counts that time
  itself.
- Each piece of time belongs to the model request it leads to (or, at the end of a turn, the one
  before it), so it lands in that request's record: the same model, UTC day and issue as its
  tokens. Time before a mark goes to the issue of the request after it, a piece is never cut at
  midnight (spans may start a few minutes before the record's day), and duplicated or resumed
  transcripts count once.
- A record's `active_seconds` is the sum over its threads (two agents busy for ten minutes are
  twenty minutes); `active_spans` is their union (ten minutes of wall clock). Specivo shows both.
- A record of a measured session with no countable time sends `0` and no spans. More than 1000
  spans in one record (rare) are merged where they lie less than a minute apart and the shortest
  are then left out; the seconds stay exact.

The three properties go only to a Specivo server that lists all three in its `optional_fields`
(Specivo 0.16.0 or later); an older server gets the records without them. When a server starts
listing them, the next push sends every record whose transcript still exists again, now with its
time. Once sent, they keep being sent: a record sent again without them clears the stored time.

| Tool | Active time |
|---|---|
| Claude Code | Yes, main thread and subagents |
| Codex | Yes, main thread and subagents, from rollouts written since September 2026 (they log each item, a reasoning step, a message or a tool call, with its start and end); older rollouts carry no active time |
| Gemini CLI, OpenCode, Hermes, `import ccusage` | Not yet: their records carry no active time |

What is read for it: Claude Code's prompts, answers, tool calls and results (their times, ids, the
tool's name and whether a person approved the call); Codex's turn starts and ends
(`task_started`, `task_complete`, `turn_aborted`) and completed items (their times, ids and type, and
the name of a collaboration tool). Never a command, an argument, an output or a message.

`ai-usage doctor` shows, per source, the hours measured in the last 7 days and the seconds it could
not give to any request (they are not sent). `active_time = false` in the config sends none of the
three properties.

### Check the measured time before pushing

`ai-usage time` prints the measured time from your logs, per issue, without sending anything or
asking the server:

```bash
ai-usage time                         # the last 7 UTC days, per issue
ai-usage time --since 2026-11-01 --until 2026-11-15 --by day
ai-usage time --by session --json     # durations in seconds
```

It collects as a push does (same sources, `exclude_paths`, deduplication and issue segments) and
reads the numbers from the records a push would send, so they are the values Specivo gets. Each
row shows the **wall clock** (the union of the agents' active intervals), the **agent time** (their
sum: two agents busy for ten minutes are twenty minutes), the number of sessions, and the records
that are **not measured** (sources without active time, see the table above; they count apart, not
as zero). The total's wall clock is the union of everything, not the sum of the rows. `--by` takes
`issue` (the issue the record names, else its project, else `(unassigned)`), `day` (UTC), `session`
(the opaque session id as sent) or `tool`; `--source` limits it to one source.

It shows issue and project keys, days, tool names and session ids, never a path, branch or prompt.
With `active_time = false` it still measures (nothing leaves the machine) and says the values would
not be sent; it also says so when the server took no active time at the last push.

## Work activity

Specivo's Agents tab shows what an agent did in Specivo. The work itself (files read and edited,
commands and tests run, subagents) happens on the agent's machine. The reporter summarises it per
session, thread (the main conversation or a subagent), UTC day and issue segment, from the same
pass over the transcript as [active time](#active-time), and sends each summary as a work activity
record (contract `specivo.agent-activity.v1`) to its own endpoint, after the usage records and with
the same API key. Specivo shows it next to the agent's Specivo steps, marked as reported by the
reporter.

What a record holds at the default level, `counts` (counts, durations and times only):

| Field | Content |
|---|---|
| `categories` | Per category, the number of tool runs and their seconds. Categories: `read`, `search`, `edit`, `shell`, `test`, `git`, `web`, `subagent`, `mcp`, `other` |
| `files_changed`, `lines_added`, `lines_removed` | Distinct files edited (counted on this machine by a hash of the path, which is never sent or stored), and the lines added and removed by the patches |
| `test_runs` | Commands classified as test runs, and how many of them failed |
| `subagents_spawned` | Subagents the thread started |
| `timeline` | Up to 300 segments `[start, end, category]`: the active time, each part labelled with the category of the tool run covering it |
| `thread`, `thread_kind` | A hash naming the thread (none for the main thread), and `main` or `subagent` |

How it is worked out (definition version 1):

- **Categories.** Claude Code: `Read` read; `Grep`, `Glob`, `ToolSearch` search; `Edit`,
  `MultiEdit`, `Write`, `NotebookEdit` edit; `WebFetch`, `WebSearch` web; `Agent`, `Task`,
  `SendMessage` subagent; MCP tools mcp; `Bash` by its command; anything else other. Codex:
  commands by Codex's own parse (read, or search for searches and file listings) and otherwise by
  the command; file changes edit; MCP calls mcp; web searches web; image views read;
  collaboration calls subagent.
- **Commands.** The command line is split at `;`, `&&`, `||`, `|` and `&`; environment
  assignments and wrappers (`sudo`, `env`, `time`, `timeout`, `uv run`, `poetry run`, `npx`,
  `python -m`, `sh -c` and the like) are skipped, and the program's name decides: `git` is
  `git`; test runners (`pytest`, `tox`, `jest`, `vitest`, `go test`, `cargo test`, `npm test`,
  `make test` and the like) are `test`; anything else is `shell`. A line counts once, under the
  most specific category found (`test`, then `git`, then `shell`). The command is read in memory
  to decide this and then dropped.
- **Seconds.** A run counts the part of its time that is active time (up to 60 minutes, cut at
  the end of its turn). A question to you counts nothing, nor does a wait for a subagent (the
  subagent counts its own work); parallel runs each count their own seconds.
- **Failures.** Claude Code's `is_error` on the result; Codex's exit code or failed status. A
  failed test run counts in `test_runs.failed`; a failed edit changes no file.
- **Lines.** Claude Code: the `+` and `-` lines of the edit's patch (a new file counts all its
  lines); Codex: the same from each change's diff (an added file counts its lines).
- **Day and issue.** A run belongs to the request that owns its start, exactly as active time
  gives each piece of time to a request, so it lands on that request's UTC day and issue segment.
  Each record names, in `usage_key`, the usage record of its session, day and segment with the
  earliest request, and Specivo takes the issue, the project and who may see it from that entry.
- **Timeline.** The active time of the group, each part labelled by the run covering it (the most
  specific category where runs overlap); time between runs, the model working, takes the label
  of the run before it (else after it). Neighbours with the same label are merged, a segment under
  30 seconds that touches another is folded into the longer neighbour, and over 300 segments the
  closest are joined.

### Levels

```toml
[activity]
level = "counts"            # off | counts | names | commits

[activity.repos]            # repository patterns as in [projects]; the first match wins
"github.com/client-x/*" = "off"
```

| Level | Sends |
|---|---|
| `off` | Nothing: no work activity record at all |
| `counts` (default) | The fields above: counts, durations, line totals, the labelled timeline. No names of anything |
| `names`, `commits` | Accepted, but not built yet: such sessions send `counts` records. A later release adds command names, repository-relative paths, MCP server names and subagent types (`names`), and commit SHAs and subjects (`commits`) |

The repository is the one the usage records of the group name (`host/owner/repo`, from git or the
tool's log); when they name several, the lowest level wins. A session whose repository is unknown
(git enrichment off, no remote, no git) could be any of them, so it gets the lowest of the global
level, `counts` and every level in `[activity.repos]`: one `"off"` pattern holds back every session
without a known repository. Switching a repository to `off` stops sending its activity; what was sent before stays in
Specivo.

**Never sent at any level:** prompts, model output, file contents, diffs, command arguments,
environment variables, absolute paths, paths outside the repository, tool output, URLs fetched,
search queries. The record is built field by field from an allowlist; a field above the record's
level is never built (Specivo rejects a record that has one).

### Sending

- Nothing is sent unless the server answers `GET /api/v1/agent-activity/ingest` (Specivo 0.16.0
  or later): a server without it (404) gets no activity, and the usage push is not affected. The
  default level `counts` therefore only takes effect against such a server.
- Usage goes first, then activity, in batches under the endpoint's limits (500 records, 1 MiB).
  A record is sent only once its usage record is in the ledger as sent, and again only when it
  changed.
- A record the server could not anchor yet (`usage_not_found`) or could not store
  (`internal_error`) is sent again on the next push; one it refused (`invalid_record`) is not, until
  it changes. When a session's issue split changes, a group sent before that no longer exists is
  sent again empty, so Specivo does not keep its old counts.
- `ai-usage show-payload` and `push --dry-run` print the activity records a push would send, after
  the usage records, in their own `specivo.agent-activity.v1` envelope (with a heading on standard
  error). `push --sink json --output usage.json` writes them to `usage.activity.json`.
- `ai-usage doctor` shows the level, the per-repository patterns, the records per source and how
  many groups a level of `off` left out, and whether the server takes work activity.

| Tool | Work activity |
|---|---|
| Claude Code | Yes, main thread and subagents |
| Codex | Yes, main thread and subagents, from rollouts written since September 2026 |
| Gemini CLI, OpenCode, Hermes, `import ccusage` | Not yet |

## Route and provider

`route` names the backend the requests went through, as the tool names it: the ChatGPT login, a
company gateway, a provider profile. Specivo uses it to tell who pays (see billing accounts in
Specivo 0.15.0 and later); it never changes the price. `provider` stays the model's maker, which
Specivo prices by. Both are sent only to servers that list `route` in `optional_fields`.

| Tool | `route` | `provider` |
|---|---|---|
| Codex | `model_provider` of the session (`openai`, or the id of a `[model_providers.<id>]` profile in `config.toml`) | `openai` for OpenAI's models (`gpt-*`, `o*`, `codex-*`) whatever the profile; a local engine (`ollama`, `lmstudio`) gives `local` and `cost_type = local`; other models the maker the profile or the model id names, else none (Specivo prices by model name) |
| OpenCode | `providerID` (`anthropic`, `openrouter`, `github-copilot`, a custom provider) | a known maker's id (`anthropic`, `openai`, `deepseek`, `zhipuai`, `moonshotai` and the like, under Specivo's names); through a gateway such as `openrouter` the model id's prefix; for a bundle or custom provider none, so Specivo prices by model name |
| Claude Code | the host of `ANTHROPIC_BASE_URL` (a gateway), else `claude.ai:org-<hash>` for a claude.ai login | `anthropic` |
| Hermes | the billing provider (see [Hermes Agent](#hermes-agent)) | the model's maker |

Codex rollouts written before Codex logged `model_provider` have no route and keep `openai`.

**Claude Code** does not log the endpoint it called, so the reporter looks, in this order:

1. The host of `ANTHROPIC_BASE_URL` that the `SessionStart` hook saw (exact per session; install
   the hooks to get it). The hook keeps only the host, in the local state database. A session it
   saw without a base URL skips step 2.
2. `env.ANTHROPIC_BASE_URL` in the session directory's `.claude/settings.local.json` and
   `.claude/settings.json`, then in `~/.claude/settings.json` (or `CLAUDE_CONFIG_DIR`). This is
   today's value, a best guess for older sessions. These `env` blocks hold API tokens: the reporter
   drops every other key while it parses the file and keeps only the host, never the URL, user,
   password, port or path.
3. The organization of the claude.ai login, from the transcript's `session-owner` line (recent
   Claude Code versions), as `claude.ai:org-` and the first 8 hex digits of its SHA-256. It tells
   a personal login from a company Team or Enterprise login; the organization id itself is never
   sent.

Without any of them the route is left out.

## Hermes Agent

The `hermes` source reads Hermes Agent's `state.db`: the default profile's in the Hermes home and
each other profile's in `profiles/<name>/`. The home is `HERMES_HOME`, else `~/.hermes`
(`%LOCALAPPDATA%\hermes` on Windows); a `HERMES_HOME` that names a profile reads the whole tree.
`[sources.hermes] roots` replaces the default homes and may name a database file.

**Growth, not requests.** Hermes keeps running totals per session, model, billing route and task
(its auxiliary calls such as titles and compression are counted too), not a row per request. Each
push compares them with the totals it saw last (kept in the reporter's state database) and counts
the growth on the UTC day Hermes last saw the usage (never later than the push). Pushing again,
or reading a copy of a database, counts nothing twice. A session that runs past midnight UTC can
land up to one push interval late on the earlier day. A first push counts each session's whole
history on the day of its last use. Totals that went down (a repair) count their current values.
`show-payload` and `push --dry-run` work on a copy of the state, so they show what a push would
send: records unchanged since the last push and records the server refused for good are left out
(stderr says how many), and the optional fields the server did not accept at the last push are
dropped as a push drops them. `--all` prints every collected record.

**Sessions.** Subagent, branch, compression and reset children are followed to their root session
and counted there; `push --session <child id>` selects the root. `parent_session_id` is therefore
never sent by this source.

**Route and provider.** `route` is Hermes's billing provider as it is (`openrouter`, `nous`,
`openai-codex`, `anthropic`, `custom`). `provider` is the model's maker, which Specivo prices by:
through a gateway (`openrouter`, `litellm`) the model id's prefix (`anthropic/claude-sonnet-4.6`
gives `anthropic`; Specivo matches `4.6` and `4-6`), `openai-codex` gives `openai`, `nous` stays
`nous`, and a local engine (`ollama`, `vllm` and the like) or an endpoint on this machine or a
private network gives `local`.

**Cost type.** Usage Hermes marks `subscription_included` (the Codex and Portal subscriptions) is
sent as `cost_type = subscription`, local engines as `local`; otherwise Specivo decides. A
`[[cost_rules]]` entry wins over both.

**Profile and unattended.** `profile` is the root session's Hermes profile (`default` for the home
itself). A root session Hermes started from cron is sent as `unattended`.

**Schema changes.** Hermes changes its database often. Every read checks `schema_version` and the
columns it uses. A database older than schema 20, or one missing a column the reporter needs, is
skipped with the message `Hermes schema changed, update the reporter` in `push` and `doctor`, and
the other sources go on. A schema newer than the one the reporter was tested with (33) is read by
its columns, with a note. Optional columns degrade: without `profile_name` the profile comes from
the directory, without readable tool calls there are no issue marks.

**When to push.** `ai-usage schedule install` pushes every 30 minutes, which also covers cron runs
and gateways with nobody at a terminal. To push when an interactive session ends as well, add a
shell hook to Hermes's `config.yaml` (Hermes asks you to approve it the first time):

```yaml
hooks:
  on_session_end:
    - command: ai-usage push --source hermes
      timeout: 60
```

**Docker and other hosts.** Run the reporter where it can read the Hermes data directory: on the
host, with the container's data directory bind-mounted read-only, and
`[sources.hermes] roots = ["/opt/data"]` (the mount point); or inside a remote host's own
environment (the reporter needs only Python 3.11, no packages). Reading a copy of `state.db` taken
from another host also works, but each copy is read as it was when it was taken.

## Days are UTC

A record is one **bucket**: one session of one tool, with one model, on one **UTC** day. A session
that runs past midnight UTC (07:00 in Bangkok, for example) becomes two records. This keeps
Specivo's price and exchange-rate lookups the same for everyone. Specivo shows days in each
viewer's own time zone; the `timezone` setting only changes how `ai-usage status` shows times.

## Costs

The reporter sends tokens; Specivo computes the cost. Tell it how the usage is paid, and which
billing account pays for it, with `[[cost_rules]]` in the config:

```toml
# Codex through the company gateway: the company's account pays.
[[cost_rules]]
tool = "codex"
route = "corp-*"
billing_account = "corp-gateway"

# Everything run in the client's folders is billed to the client's account.
[[cost_rules]]
cwd = "~/work/client-a"
billing_account = "client-a"

[[cost_rules]]
tool = "claude-code"
cost_type = "subscription"
subscription_hint = "claude-max-20x"

[[cost_rules]]
repo = "github.com/acme/*"
cost_type = "api"
```

Rules are tried in order and the **first rule whose filters all match** sets the outputs; later
rules are not consulted, so put narrow rules (a route, a folder) before tool-wide ones. A rule
without filters matches everything.

| Filter | Matched against | How |
|---|---|---|
| `tool` | the tool slug (`claude-code`, `codex`, `opencode`, `hermes`, ...) | glob, case-sensitive |
| `model` | the model id | glob, case-insensitive |
| `repo` | the repository as `host/owner/repo` | glob, case-insensitive; no match when the bucket has no repository |
| `route` | the [route](#route-and-provider) (Codex `model_provider`, OpenCode `providerID`, Claude Code base URL host or `claude.ai:org-<hash>`, Hermes billing provider) | glob, case-insensitive; no match when the source sent no route |
| `cwd` | the working folder of the session | glob on the folder **or any folder above it**, so a plain path such as `~/work/client-a` covers everything under it and `~/work/client-*` covers every client folder; `~` is expanded; case-sensitive except on Windows; no match when the source logged no folder |

Globs are shell-style: `*` (any text, including `/`), `?` and `[abc]`.

A bucket (one session, model and UTC day) can hold requests from more than one folder, for example
when the agent changed directory. The `cwd` filter then sees the folder **most of its requests ran
in** (ties go to the first in sort order), the same folder the repository hint comes from, so a
bucket is billed to one account. The folder is only matched locally; it is never sent.

| Output | Meaning |
|---|---|
| `cost_type` | `subscription`, `api`, `local`, `free` or `unknown`; wins over what the tool says |
| `subscription_hint` | which of your subscriptions covers it |
| `billing_account` | the slug of a Specivo billing account (`a-z`, `0-9`, `.`, `_`, `-`, up to 64 characters, starting with a letter or digit; checked when the config is loaded). Specivo looks it up among your own accounts and the instance's. Sent only to servers that list it in `optional_fields` |

**Gateways and tool-wide rules.** A rule that names only a tool (`tool = "codex"` →
`subscription`, `tool = "opencode"` → `api`) also catches the usage that went through a corporate
gateway, which is then reported as your own subscription or API spend. Scope such rules by `route`,
or put a gateway rule with its `billing_account` before them.

Records imported from ccusage have no route and no folder, so rules with `route` or `cwd` never
match them.

Without a rule, Specivo uses `subscription` when you have an AI subscription covering the tool,
and `unknown` otherwise.

When a tool logs what each request cost (OpenCode does, and Hermes for OpenRouter, in US dollars;
Hermes's own estimates are never sent), the bucket's sum is sent
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
  repository as `host/owner/repo`, commit SHAs, the issue from `SPECIVO_ISSUE` or the issue key
  the agent marked with its Specivo tool calls, the project key from your mapping, cost hints,
  `on_behalf_of`, and (unless `active_time = false`) the agent's active seconds and the
  second-precision spans of when the agent was active.
- **Sent as work activity** (level `counts`, unless `[activity] level = "off"`; only to a server that
  takes it): per session, thread, day and issue segment, the number of tool runs and their seconds
  by category, the number of files changed, lines added and removed, test runs and failures,
  subagents started, a timeline of category-labelled segments, and a hashed thread id. No names of
  anything: no command, path, file name, MCP server or commit. See [Work activity](#work-activity).
- **Never sent, printed or logged:** prompts, responses, code, tool input and output, file paths,
  working directories, file names, remote URLs with credentials.
- The record sent is built from an allowlist and checked field by field against the contract.
- `ai-usage show-payload` and `push --dry-run` show exactly what a push would send now.
- The local state database keeps the paths of the log files it read, to skip unchanged ones, and
  the records it sent (usage and work activity). For work activity it keeps only categories, counts
  and hashes of edited files' paths, never a command, path, patch or output. It stays on your
  machine.
- `exclude_paths` in the config keeps sessions that ran in those directories out entirely.
- For the Claude Code route, only the host of `ANTHROPIC_BASE_URL` is read from Claude Code's
  settings files and hook environment (those hold API tokens; nothing else is kept), and the
  claude.ai organization id is sent only as an 8-digit hash.
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
| `agent_log_issues` | Split long sessions by the issues the agent marked with its Specivo tool calls and send them (default `true`); see [Long sessions on several issues](#long-sessions-on-several-issues) |
| `active_time` | Send the measured active time of each record (seconds and spans, default `true`); see [Active time](#active-time) |
| `exclude_paths` | Sessions that ran in these directories are never reported |
| `[activity]` | `level = "counts"` (default), `"off"`, `"names"` or `"commits"` (the last two send `counts` for now): how much work activity to send; see [Work activity](#work-activity) |
| `[activity.repos]` | Repository pattern (as in `[projects]`) → work activity level; the first match wins. A session without a known repository gets the lowest of `level`, `counts` and every level listed here |
| `[sources.<name>]` | `enabled = false` turns a source off; `roots = [...]` reads other directories; `via = "ccusage"` counts the tool through `import ccusage` instead of its logs; `project = "KEY"` is the tool's project when no `[projects]` repository pattern matches (for example sessions outside any git repository) |
| `[[cost_rules]]` | `tool`, `model`, `repo`, `route`, `cwd` (patterns) → `cost_type`, `subscription_hint`, `billing_account`; the first match wins. See [Costs](#costs) |
| `[projects]` | Repository pattern (`host/owner/repo`, `*` matches `/` too) → Specivo project key; map every repository you work in, see [Keep other work out of Specivo issues](#keep-other-work-out-of-specivo-issues) |
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
| Usage from another repository shows up on an issue (attribution `mcp_session`) | Another agent session worked on that issue at the same time; map the repository in `[projects]` or exclude its folder (see [Keep other work out of Specivo issues](#keep-other-work-out-of-specivo-issues)), then move the entry in Specivo |
| `push` exits with status 1 | Some records are waiting for a retry (server busy or offline); the next push sends them |
| `work activity: the server takes none` | The server has no work activity endpoint (Specivo before 0.16.0): nothing is sent, usage is not affected |
| Work activity `waiting for their usage record` | Their usage record was not accepted yet; they go once it is |
| Codex rollouts are skipped | Install the `zstd` extra |
| `doctor` says an OpenCode database is busy | OpenCode was writing; the next push reads it |
| `Hermes schema changed, update the reporter` | Hermes changed `state.db` in a way this reporter cannot read; that database is skipped until you update the reporter (the other sources go on) |
| `Hermes state.db schema vN is newer than ...` | A note only: the database is read by its columns. Update the reporter when a release covers that schema |
| `Hermes usage counters went down` | Hermes repaired or reset a session's totals; their current values were counted as new usage |
| The time an issue shows in Specivo looks wrong | Run `ai-usage time` (add `--by session` or `--by day`) to see the measured time the next push would send, before pushing |
| A hook does not seem to run | Look at `hooks.log` in the state directory; run `ai-usage hooks install --tool <tool>` again after moving the reporter |

## Writing an adapter

The reporter reads other tools through plugins. A plugin package implements the `Adapter`
protocol, registers it under the entry-point group `ai_usage_reporter.adapters` and passes the
conformance kit in `ai_usage_reporter.testing`. The checklist is in
[CONTRIBUTING.md](https://github.com/specivo/ai-usage-reporter/blob/main/CONTRIBUTING.md).
