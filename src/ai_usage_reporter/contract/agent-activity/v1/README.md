# Agent activity ingest contract, version 1

This contract describes how a program reports a **summary of an AI agent's work on its own
machine** (tool runs by category, files edited, tests run, subagents, and at higher privacy
levels command names, file paths and commits) to a Specivo server. Specivo shows it next to the
agent's own Specivo actions; it never mixes the two.

It is a companion of the [AI usage ingest contract](../../ai-usage/v1/README.md): every activity
record is anchored to a usage record the same program has already sent. It is separate so that
a reporter or a server without it is unaffected, and so that the privacy level can differ per
repository.

- Contract version: `specivo.agent-activity.v1`
- Schemas (JSON Schema draft 2020-12), `$id` `https://specivo.io/schemas/agent-activity/v1/<file>`:

| File | Describes |
|---|---|
| [`batch.schema.json`](batch.schema.json) | The request body of `POST` |
| [`record.schema.json`](record.schema.json) | One activity record inside a batch |
| [`result.schema.json`](result.schema.json) | The `200` response body of `POST` |
| [`info.schema.json`](info.schema.json) | The `200` response body of `GET` |

- Examples: [`examples/`](examples/), with the answers a Specivo server gives in
  [`examples/expected.json`](examples/expected.json).

Where this page and a schema disagree, one of them has a bug. Please report it.

## Contents

1. [Endpoints and authentication](#endpoints-and-authentication)
2. [The batch](#the-batch)
3. [The record](#the-record)
4. [Privacy levels](#privacy-levels)
5. [Never accepted](#never-accepted)
6. [Groups, anchors and upserts](#groups-anchors-and-upserts)
7. [Reporting for someone else](#reporting-for-someone-else)
8. [The response](#the-response)
9. [Limits](#limits)
10. [Compatibility rules](#compatibility-rules)

## Endpoints and authentication

| Method and path | Purpose |
|---|---|
| `GET /api/v1/agent-activity/ingest` | What the server accepts: versions, limits, its clock, levels, categories |
| `POST /api/v1/agent-activity/ingest` | Send a batch of activity records |

The URL with a trailing slash works too. Authentication is the usage contract's: an API key in
`Authorization: Bearer spv_...`, whose account is the **reporter** of every record.

A server that answers `GET` with 404 does not take activity: a reporter sends none to it. Push
usage first, then activity, so that every anchor exists.

`GET` returns (see `info.schema.json`):

```json
{
  "versions": ["specivo.agent-activity.v1"],
  "max_records": 500,
  "max_bytes": 1048576,
  "max_age_days": 400,
  "rate_limit": {"requests": 60, "window_seconds": 60},
  "server_time": "2026-11-06T12:00:00.123456Z",
  "levels": ["counts", "names", "commits"],
  "categories": ["read", "search", "edit", "shell", "test", "git", "web", "subagent", "mcp", "other"],
  "limits": {"timeline": 300, "mcp_servers": 50, "commands": 100, "files": 200, "commits": 100},
  "optional_fields": []
}
```

`optional_fields` lists record properties added within v1 that the server accepts; send such a
property only to a server that lists it. The first revision has none.

## The batch

```json
{
  "schema": "specivo.agent-activity.v1",
  "client": {"name": "ai-usage-reporter", "version": "1.4.0"},
  "records": [ { ... } ]
}
```

`schema` (required, the contract version), `client.name` (required, 1 to 100 characters),
`client.version` (up to 50) and `records` (1 to 500), exactly as in the usage contract. The
client name and version are not stored.

## The record

One record is the **complete** work activity summary of one group: one thread of one agent
session, on one UTC day, for one issue segment (the same split as usage, plus the thread, so a
subagent has its own record).

Required: `idempotency_key`, `usage_key`, `tool`, `session_id`, `usage_date`, `level`,
`activity_version`, `categories`. Every other property may be left out or set to `null`.

| Property | Level | Meaning |
|---|---|---|
| `idempotency_key` | any | Names the group for its target user; 1 to 128 printable ASCII characters, no spaces. Recommended: the lower-case hex SHA-256 of `v1\|activity\|<tool>\|<session_id>\|<thread or ->\|<usage_date>\|<issue or ->`. |
| `usage_key` | any | The `idempotency_key` of a usage record of the same tool, session and day (and issue segment), sent before by the same account for the same target user: the one with the earliest request. See [anchors](#groups-anchors-and-upserts). |
| `tool` | any | Tool slug (`claude-code`, `codex`); must be the anchor entry's tool. |
| `session_id` | any | Opaque session id, 1 to 200 characters; must be the anchor entry's `session_id`. |
| `usage_date` | any | The UTC day, `YYYY-MM-DD`; must be the anchor entry's day. Not in the future, at most 400 days old. |
| `level` | any | `counts`, `names` or `commits`: the level the record was built at. See [Privacy levels](#privacy-levels). |
| `activity_version` | any | Version of the summary definition; `1`. Stored, never interpreted. |
| `thread` | counts | Opaque id of the thread: a hash in hex or base64url, 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`, never a name or path; `null` for a session's only thread. |
| `thread_kind` | counts | `main` (default) or `subagent`. |
| `categories` | counts | Tool runs and their summed seconds by category: `{"edit": {"count": 23, "seconds": 140}, ...}`. Keys only from the fixed list `read`, `search`, `edit`, `shell`, `test`, `git`, `web`, `subagent`, `mcp`, `other`; another key **rejects** the record. A category without runs may be left out. `{}` is valid. |
| `files_changed` | counts | Number of distinct files edited. |
| `lines_added`, `lines_removed` | counts | Line totals from the patches. |
| `subagents_spawned` | counts | Number of child threads started. |
| `test_runs` | counts | `{"count": 4, "failed": 1}`: commands classified as test runs, and those that ended with an error. `failed <= count`. |
| `timeline` | counts | At most 300 segments `[start, end, category]`: the thread's active spans labelled with the category that took most of each. RFC 3339 timestamps with an offset, `start < end`, sorted, not overlapping (a segment may start where the previous one ends), within 24 hours of `usage_date` and not in the future. |
| `mcp_servers` | names | At most 50 `{"server", "count", "seconds"}`; `server` is an MCP server name of 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `_`, `.` and `-`, starting with a letter or digit. Each server once. |
| `commands` | names | At most 100 `{"name", "count", "failed"}`. `name` is a program's base name, for `git` also its verb: it must match `^[a-z0-9][a-z0-9._+-]{0,31}( [a-z][a-z-]{0,23})?$` (`pytest`, `make`, `git commit`). Never arguments. Each name once, `failed <= count`. |
| `files` | names | At most 200 `{"path", "edits", "added", "removed"}`. `path` is relative to the repository root, 1 to 255 characters: no leading `/`, `\` or `~`, no `..` segment, no `:` (so no drive letter and no URL), no backslash, no control characters (C0 or C1) and no bidirectional formatting characters (U+202A to U+202E, U+2066 to U+2069). Each path once. |
| `files_outside` | names | Edited files that were counted but not named: outside the repository, or withheld by the reporter's patterns for secret-looking names. |
| `agent_type` | names | The subagent's type name: 1 to 100 characters of letters, digits, space, `_`, `.`, `:`, `@`, `+` and `-`, starting with a letter or digit (no `/`). |
| `commits` | commits | At most 100 `{"sha", "branch", "subject", "committed_at"}`. `sha` 40 or 64 hex characters (stored in lower case, each once), `branch` up to 255 characters, `subject` the first line up to 200 characters, both without control characters (C0 or C1) or bidirectional formatting characters, `committed_at` RFC 3339 with an offset. Only `sha` is required. |
| `on_behalf_of` | any | Login of the person the activity belongs to, as in the usage contract. See [Reporting for someone else](#reporting-for-someone-else). |

All counts are whole numbers from 0 to 10^9.

## Privacy levels

The reporter's configuration chooses a level per repository; the default is `counts`. A level
includes everything of the levels before it.

| Level | Adds |
|---|---|
| `counts` | `thread`, `thread_kind`, `categories`, `files_changed`, `lines_added`, `lines_removed`, `subagents_spawned`, `test_runs`, `timeline`: counts, durations, line totals and the labelled timeline. No names of anything. |
| `names` | `mcp_servers`, `commands`, `files`, `files_outside`, `agent_type`: MCP server names, command names without arguments, repository-relative paths, subagent type names. |
| `commits` | `commits`: SHAs, branches and subject lines. |

(`off` is a reporter setting: nothing is sent.)

**The server enforces the level.** A record holding a property of a higher level than its
`level` is rejected with `invalid_record` and `field` naming that property. Presence counts,
even with the value `null`: a reporter builds only the fields its level allows, so a stray key is
a bug, and nothing above the configured level is ever stored. This check comes before every
other check of the record.

## Never accepted

No property of this contract can carry, at any level: prompts, model output, file contents,
diffs, command arguments, environment variables, absolute paths, paths outside the repository,
tool output, fetched URLs or search queries. The only free text is the commit `subject` and
`branch` (`commits` level only). Properties the server does not know are ignored with
the warning `unknown_fields` (which repeats at most ten names, each cut to 64 characters) and are **never stored**; a server rejects any value that breaks
the patterns above **without echoing it** in the error.

## Groups, anchors and upserts

A record is keyed by **(target user, `idempotency_key`)** and is the complete state of its
group. Sending it again replaces what is stored: the result is `unchanged` when nothing
differs, otherwise `updated` (the `revision` grows). A record of a lower level than the stored
one clears what only the higher level carried. The first send is `created`.

The **anchor** is the usage entry of the target user whose `idempotency_key` is the record's
`usage_key`. The activity has no project or issue of its own: Specivo reads them, and who may
see the activity, from the anchor entry, so a person moving that entry to another issue moves
the activity with it, and deleting the entry deletes its activity. The anchor must:

- exist: otherwise `usage_not_found`. Push the usage record first; send the activity again on the
  next push;
- have been reported by the same account: otherwise `key_conflict` (for one's own usage);
- be of the same `tool`, `session_id` and `usage_date`: otherwise `invalid_record` on `usage_key`.

The reporting account needs what it would need to report the anchor entry's usage now: **Log AI
usage** in the anchor entry's project (unassigned entries: one's own only).

## Reporting for someone else

`on_behalf_of` works as in the usage contract: the account needs **Log AI usage for others** in
the anchor entry's project, the person must be an active member there, and unassigned activity
for someone else is for administrators. The anchor is looked up among the person's entries, and
must have been reported by the same account.

Against login enumeration, a record for someone else never tells logins apart: it is `forbidden`
when the account may not log for others anywhere or is refused in the anchor's project, and
`usage_not_found` when no entry of that person with that key was reported by this account,
whether or not the login exists. An activity stored or changed for someone else is written to
Specivo's audit log, without its content.

## The response

A batch whose envelope is valid always gets **HTTP 200**, even if every record was rejected
(see `result.schema.json`):

```json
{
  "schema": "specivo.agent-activity.v1",
  "server_time": "2026-11-06T12:00:00Z",
  "summary": {"received": 2, "created": 1, "updated": 0, "unchanged": 0, "rejected": 1},
  "results": [
    {"index": 0, "idempotency_key": "9b1e...", "status": "created", "activity_id": 311,
     "revision": 1, "usage_entry_id": 1042, "warnings": [], "error": null},
    {"index": 1, "idempotency_key": "4c0a...", "status": "rejected", "activity_id": null,
     "revision": null, "usage_entry_id": null, "warnings": [],
     "error": {"code": "invalid_record", "message": "files needs level names or higher; this record is level counts", "field": "files"}}
  ],
  "warnings": []
}
```

**Error codes** (`error.code` of a rejected record):

| Code | Meaning | Retry? |
|---|---|---|
| `invalid_record` | The record breaks the schema, its level or a rule on this page; `field` names the property or item (`files.3.path`), never its value | No, fix it |
| `usage_not_found` | No usage entry with `usage_key` reported by this account for the target user | Yes, after pushing the usage record |
| `key_conflict` | Another account reported the anchor entry, or this activity key, for this user | No |
| `duplicate_key` | The batch already holds this key for this user | No |
| `forbidden` | The account may not log this usage there; for `on_behalf_of`, any refusal (see above) | No |
| `not_found` | The anchor entry's project cannot be reached | No |
| `target_not_member`, `target_invalid` | The person is not an active member of the anchor's project, or not an active account | No |
| `internal_error` | The server could not store the record (also when two pushes of the same key race) | Yes, later |

**Warning codes**: `unknown_fields` (properties this server does not know were ignored and not
stored). Treat unknown codes like the generic case: an unknown error means rejected, an unknown
warning means stored.

**Whole-batch errors** are the usage contract's: 400 `invalid_json`, `invalid_batch`,
`unsupported_schema` (with `details.supported`), 401, 413 `batch_too_large`, 429
`rate_limit_exceeded` with `Retry-After`.

## Limits

- At most **500 records** and **1 MiB** per request; `GET` returns both. A batch of `names` or
  `commits` records can reach the byte limit before the record limit: split by size.
- The per-record limits in `GET` `limits` (timeline 300, MCP servers 50, commands 100, files 200,
  commits 100) and the string lengths in the schema.
- Specivo allows 60 activity batches per minute per account by default
  (`AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE`), counted apart from usage batches.

## Compatibility rules

- Within v1, changes are additive: new optional record properties are listed in `GET`
  `optional_fields`; nothing required is added, nothing removed, no meaning changes.
- A server ignores record and batch properties it does not know (`unknown_fields`) and never
  stores them; the published schemas stay strict so that reporters catch their own typos.
  Unknown `categories` keys are the exception: they reject the record.
- Clients ignore response properties they do not know.
