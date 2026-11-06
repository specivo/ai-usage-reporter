# Changelog

Release notes of `specivo-ai-usage-reporter`. Install a release with
`uv tool install git+https://github.com/specivo/ai-usage-reporter@vX.Y.Z` (or `pipx install`).

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
