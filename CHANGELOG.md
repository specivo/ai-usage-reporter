# Changelog

Release notes of `specivo-ai-usage-reporter`. Install a release with
`uv tool install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z` (or `pipx install`).

## 0.4.0

Routes, billing accounts, Hermes Agent, and a preview that matches the push.

- **Hermes Agent** (`hermes` source): reads `state.db` in the Hermes home and in each profile. Hermes
  keeps running totals per session and model, so the reporter sends their growth since its last
  read and never counts twice. Child sessions count in their root session; the agent profile, the
  unattended flag (cron runs) and the route are sent.
- **Route** of each record, the backend the requests went through: Codex's `model_provider`,
  OpenCode's `providerID` (its maker is sent as the provider only when the route names it, so a
  gateway's usage is still priced), and for Claude Code the gateway host from
  `ANTHROPIC_BASE_URL` (recorded by a new SessionStart hook or read from the settings) or the
  claude.ai login.
- **`[[cost_rules]]`** can match the route and the working folder, and set `billing_account`.
- The optional record fields `route`, `billing_account`, `parent_session_id`, `unattended` and
  `profile` are sent only to servers that list them; a record sent without them goes again once
  the server does.
- `show-payload` and `push --dry-run` print exactly what a push would send now (records unchanged
  since the last push or refused for good are left out, and stderr says how many); `--all` prints
  every collected record. `doctor` reports both counts.
- Fixed: records sent without their agent_log issue to an older server go again with it once the
  server takes it; billing account slugs may contain `_` and `.`, as the server allows.

## 0.3.0

- **Issue segments**: Claude Code, Codex and OpenCode 2 transcripts keep the agent's Specivo tool
  calls, so a session's usage is split by the issue the agent was working on (status or assignee
  changes, timer starts, created issues mark an issue), sent with `issue_ref_source: agent_log`.
- A default project per source for sessions that no repository pattern maps.
- Fixed: Windows file ids that do not fit in 64 bits; `init` replacing the key with the URL already
  in the config, refusing what is not an API key, and explaining `--key-stdin` without a terminal.

## 0.2.0

New sources, and pushing without thinking about it.

- **Gemini CLI**: reads the chat recordings in `~/.gemini/tmp/*/chats/`, both the older JSON files
  and the current JSONL ones. Cached prompt tokens are reported as cache reads, thoughts as
  reasoning, and subagent sessions count in their parent.
- **OpenCode**: reads `opencode.db` (OpenCode 1 and 2 tables) in `~/.local/share/opencode`, read-only
  and without ever locking it, and the older `storage/` JSON files. Child sessions count in their
  root session, and OpenCode's per-message cost is sent as `reported_cost`.
- **`ai-usage import ccusage`**: sends the day totals of `ccusage daily --by-agent --json` for the
  tools the reporter does not read itself. `[sources.<tool>] via = "ccusage"` picks the import over
  the native adapter for a tool, so a tool is never counted twice.
- **`ai-usage hooks install|uninstall --tool ...`**: Claude Code (`SessionEnd`, `Stop`), Codex
  (`notify`, keeping an existing program), Gemini CLI (`SessionEnd`, `AfterAgent`) and OpenCode
  (a plugin on `session.idle`) push each session themselves. Hooks return at once, always exit 0,
  are debounced, and every edited file is backed up first.
- **`ai-usage schedule install|remove`**: a launchd agent, a systemd user timer or a Task Scheduler
  task runs `ai-usage push` every 30 minutes (`--every` changes it).
- `push --session` with a subagent's session id selects its parent session.
- `doctor` reports databases a tool held locked, and the tools counted through the import.
- A file whose parse failed is no longer cached as empty; it is read again on the next run.

## 0.1.0

First release: Claude Code and Codex CLI adapters, the `specivo` and `json` sinks, the conformance
kit for adapters, and `init`, `push`, `status`, `doctor` and `show-payload`. Speaks the Specivo AI
usage ingest contract `specivo.ai-usage.v1`.
