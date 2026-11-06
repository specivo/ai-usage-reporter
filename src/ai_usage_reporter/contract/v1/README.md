# AI usage ingest contract, version 1

This contract describes how a program reports AI coding-tool usage (tokens, requests, the
model, which issue it was for) to a Specivo server. It is written for two audiences: people
building a **reporter** (a program that reads local tool logs and sends usage), and people
building a **server** that accepts the same batches. Either side can be written in any
language from this page and the JSON Schemas next to it.

- Contract version: `specivo.ai-usage.v1`
- Schemas (JSON Schema draft 2020-12), `$id` `https://specivo.io/schemas/ai-usage/v1/<file>`:

| File | Describes |
|---|---|
| [`batch.schema.json`](batch.schema.json) | The request body of `POST` |
| [`record.schema.json`](record.schema.json) | One usage record inside a batch |
| [`result.schema.json`](result.schema.json) | The `200` response body of `POST` |
| [`info.schema.json`](info.schema.json) | The `200` response body of `GET` |

- Examples: [`examples/`](examples/), with the answers a Specivo server gives in
  [`examples/expected.json`](examples/expected.json).

Where this page and a schema disagree, one of them has a bug. Please report it.

## Contents

1. [Endpoints and authentication](#endpoints-and-authentication)
2. [The batch](#the-batch)
3. [The record](#the-record)
4. [Buckets and upserts](#buckets-and-upserts)
5. [Time](#time)
6. [Costs](#costs)
7. [Reporting for someone else](#reporting-for-someone-else)
8. [Issue attribution](#issue-attribution)
9. [The response](#the-response)
10. [Limits](#limits)
11. [Compatibility rules](#compatibility-rules)
12. [Examples](#examples)
13. [Open decisions](#open-decisions)

## Endpoints and authentication

Both endpoints live under the server's API root, `https://<host>/api/v1`. The URL with a
trailing slash (`/api/v1/ai-usage/ingest/`) works too.

| Method and path | Purpose |
|---|---|
| `GET /api/v1/ai-usage/ingest` | What the server accepts: versions, limits, its clock, known tools and currencies |
| `POST /api/v1/ai-usage/ingest` | Send a batch of usage records |

Authenticate with an API key in the `Authorization` header:

```
Authorization: Bearer spv_...
Content-Type: application/json
```

The key belongs to an account, and that account is the **reporter** of every record in the
batch. Usage is logged in the reporter's own name unless a record says `on_behalf_of`. A key
limited to some projects can only attribute usage to those projects. A browser session also
works (the web app's cookie), but reporters should use a key.

`GET` returns (see `info.schema.json`):

```json
{
  "versions": ["specivo.ai-usage.v1"],
  "max_records": 500,
  "max_bytes": 1048576,
  "max_age_days": 400,
  "rate_limit": {"requests": 60, "window_seconds": 60},
  "server_time": "2026-11-06T12:00:00.123456Z",
  "tools": ["claude-code", "codex", "gemini-cli"],
  "currencies": ["EUR", "THB", "USD"]
}
```

A reporter can call it once at start-up to check that the server speaks its version and to
compare clocks. `rate_limit` is `null` when the server does not limit batches.

## The batch

```json
{
  "schema": "specivo.ai-usage.v1",
  "client": {"name": "ai-usage-reporter", "version": "1.0.0"},
  "records": [ { ... }, { ... } ]
}
```

| Property | Required | Meaning |
|---|---|---|
| `schema` | yes | The contract version of this body. A version the server does not support is refused with `400 unsupported_schema`, and `details.supported` lists the ones it does. |
| `client.name` | yes | The program that sends the batch (1 to 100 characters). Stored on every entry. |
| `client.version` | no | Its version (up to 50 characters). Stored on every entry. |
| `records` | yes | 1 to 500 records. |

## The record

Only five properties are required. Everything else may be left out or set to `null`.

| Property | Required | Meaning |
|---|---|---|
| `idempotency_key` | yes | Names the bucket for its target user; 1 to 128 printable ASCII characters, no spaces. See [Buckets](#buckets-and-upserts). |
| `tool` | yes | Tool slug: 2 to 50 characters of `a-z`, `0-9` and `-`, e.g. `claude-code`, `codex`, `gemini-cli`. A slug the server does not know is added as an inactive tool, so a new tool never blocks reporting. |
| `tool_version` | no | Informational; not stored by v1 servers. |
| `provider` | no | `anthropic`, `openai`, `google`, ... Defaults to the tool's provider. |
| `model` | yes | Model name as the tool reports it (up to 200 characters). A provider prefix such as `anthropic/` is stripped. |
| `session_id` | no | Opaque id of the tool's session (up to 200 characters). Stored, never parsed. |
| `usage_date` | yes | The **UTC** day of the bucket, `YYYY-MM-DD`. See [Time](#time). |
| `timezone` | no | The person's IANA time zone, e.g. `Asia/Bangkok`. Informational; not stored by v1 servers. |
| `started_at`, `ended_at` | no | RFC 3339 timestamps with an offset. See [Time](#time). |
| `request_count` | no | Model requests in the bucket, 0 to 100,000,000. Default 0. |
| `tokens` | yes | Token totals by class of the requests **below** the model's long-context threshold, see below. A missing class is 0. |
| `long_context_tokens` | no | Token totals by class of the requests that **crossed** the long-context threshold, same classes. Disjoint from `tokens`, see [Long-context tokens](#long-context-tokens). |
| `reported_cost` | no | What the provider charged: `{"amount": "1.2345", "currency": "USD"}`. The amount is a string with at most 12 integer and 8 fractional digits; the currency must be one the server knows. |
| `cost_type` | no | `subscription`, `api`, `local`, `free` or `unknown`. See [Costs](#costs). |
| `subscription_hint` | no | The plan the tool reports, e.g. `claude-max-20x`. Informational; not stored by v1 servers. |
| `on_behalf_of` | no | Login of the person the usage belongs to. See [Reporting for someone else](#reporting-for-someone-else). |
| `attribution` | no | Hints for the issue and project. See [Issue attribution](#issue-attribution). |

**Token classes** (each a whole number from 0 to 10^12):

| Class | Counts |
|---|---|
| `input` | Input tokens, **excluding** cache reads and cache writes |
| `output` | Output tokens, excluding reasoning when the provider reports it apart |
| `cache_write_5m` | Tokens written to a 5-minute prompt cache |
| `cache_write_1h` | Tokens written to a 1-hour prompt cache |
| `cache_read` | Tokens read from a prompt cache |
| `reasoning` | Reasoning tokens reported separately from output |

Normalise before sending: some providers count cached tokens inside `input`. Subtract them,
so that every token is counted in exactly one class.

### Long-context tokens

Some models bill a request at higher prices once its context (input plus cache reads and
writes) passes a threshold, for example 200,000 tokens. Such a request is billed at the
long-context prices **as a whole**, not only for the tokens above the threshold.

`tokens` and `long_context_tokens` are therefore **two disjoint buckets**:

- `tokens` holds only the requests below the threshold.
- `long_context_tokens` holds only the requests that crossed it.
- A request goes **wholly into one bucket**: all its classes (input, output, cache reads and
  writes, reasoning) are counted in the same bucket, never split between the two.
- `long_context_tokens` is **not** a part of `tokens`. Do not count a long-context request in
  `tokens` as well.
- The usage of the record is the **sum** of both buckets, class by class: its input is
  `tokens.input + long_context_tokens.input`, and so on.

The server prices `tokens` at the base prices and `long_context_tokens` at the long-context
prices, so each token is priced once. Leave `long_context_tokens` out (or `null`) when the
model has no long-context tier or no request crossed it.

Example: three requests with 50,000, 120,000 and 260,000 tokens of context on a model whose
threshold is 200,000. The first two go into `tokens`, the third (its input, cache and output
tokens alike) into `long_context_tokens`.

## Buckets and upserts

A record is the **complete totals** of one bucket: one session of one tool, with one model,
on one UTC day. When a session spans midnight UTC or switches models, it becomes several
records.

- The server keys each bucket by **(target user, `idempotency_key`)**. The target user is the
  reporter, or the `on_behalf_of` user.
- Sending a record whose key is new **creates** an entry (`created`).
- Sending it again **replaces** the stored totals; it never adds to them (`updated`). Send the
  whole bucket every time, not the increment since the last send.
- If nothing changed, nothing is written (`unchanged`). Resending is therefore cheap and safe:
  a reporter can resend a whole day after a crash or a network error.
- Every change increases the entry's `revision`.
- A person can **exclude** an entry from reports in Specivo. Later sends update its totals but
  never include it again.
- Keys belong to the account that first reported them. If another account sends a record with
  the same key for the same target user, the record is rejected with `key_conflict`.
- The same key twice in one batch (for the same target user): the second is rejected with
  `duplicate_key`.

Recommended key: the lower-case hex SHA-256 of `v1|<tool>|<session_id>|<model>|<usage_date>`.
It is stable across resends, distinct per bucket, and reveals nothing about the session.

## Time

- `usage_date` is a day in **UTC**, not in the person's time zone. It must not be in the future
  (the server allows its clock to be 10 minutes behind yours) and must not be more than
  400 days ago.
- `started_at` and `ended_at` are RFC 3339 timestamps **with an offset**: `2026-11-05T02:10:00Z`
  or `2026-11-05T09:10:00+07:00`. A timestamp without an offset is rejected. Fractions of a
  second are allowed, up to 6 digits. The server stores them in UTC.
- `started_at` must fall on `usage_date` in UTC: from `00:00:00Z` up to, but not including,
  `00:00:00Z` the next day. Example: `2026-11-05T06:30:00+07:00` is `2026-11-04T23:30:00Z`, so it
  belongs to `usage_date` `2026-11-04`.
- `ended_at` must fall on the same day, or be exactly `00:00:00Z` of the next day (the end of
  the bucket). It must not be before `started_at`.
- Neither may be in the future.
- Reports in Specivo show days in each viewer's time zone. Because buckets are UTC days, a local
  day can differ from the UTC day by a few hours at its edges.

## Costs

The reporter sends tokens. The **server** computes costs from its own price table, so that
everyone is priced the same way.

- Each entry is priced at one instant, `priced_at`: its `started_at`, or `00:00:00Z` of
  `usage_date` when there is no `started_at`. The server uses the price that was valid at that
  instant, never simply the latest price.
- `cost_type` says how the usage is paid:

| `cost_type` | Spend (marginal cost) |
|---|---|
| `api` | `reported_cost` when sent, otherwise the computed API price |
| `subscription` | 0: the plan is paid anyway |
| `local` | 0, or the local model's price if an administrator set one |
| `free` | 0 |
| `unknown` | Unknown |

- Leave `cost_type` out (or `null`) to let the server decide: if the user has an AI subscription
  covering the tool (or its provider) at `priced_at`, it is `subscription`, otherwise
  `unknown`.
- `reported_cost` is stored for every cost type, but it is the spend only for `api`.
- The server also stores what the usage would have cost at API prices (the "API equivalent"),
  whatever the cost type.

## Reporting for someone else

A shared service account can report usage for several people with `on_behalf_of`, the login of
the person (case does not matter). The rules:

- The reporting account needs the permission **Log AI usage for others** in the project the
  record is attributed to, and the person must be an **active member** of that project.
- Usage without a project for someone else can only be reported by an administrator.
- A login that matches no account is `target_unknown`; an inactive or system account is
  `target_invalid`; a person who is not an active member of the project is
  `target_not_member`; a missing permission is `forbidden`.
- So that `on_behalf_of` cannot be used to find out which logins exist, these codes are
  only told apart for an administrator, or for an account that holds **Log AI usage for
  others** in the record's explicit project (`attribution.project_key`, or the project of
  `attribution.issue_ref`). Every other account gets `forbidden` with the same message
  ("You cannot log AI usage for this user here") whether the login exists or not, whether the
  person is active, a member or neither. Such a record is either stored (when it ends up
  attributed to a project where the account may log for others) or rejected with this
  `forbidden`, whatever the reason: an unusable `project_key` or `issue_ref` and a
  `key_conflict` included. Its warnings found on the way (`attribution_dropped`,
  `attribution_ambiguous`) are left out. Errors in the record's content
  (`invalid_record`, `currency_unknown`, `duplicate_key`) come first and do not depend on
  the login. An account that may log for others in no project at all gets this `forbidden`
  for every `on_behalf_of` record but its own login. Send `project_key` (or `issue_ref`)
  with on-behalf records to get the precise codes.
- Every entry shows who reported it and with which key. Logging for someone else is recorded in
  the security audit log, and every person sees what was logged in their name.

Usage in one's own name needs the permission **Log AI usage** in the project, or no permission
at all when the usage is not attributed to a project.

## Issue attribution

The server decides which issue (and project) a record belongs to by trying these steps in
order. The first step that gives something the target user can log to wins. The step is
stored with the entry as its `attribution_method`.

| Step | Hint | `attribution_method` | Kind |
|---|---|---|---|
| 1 | `attribution.issue_ref`, e.g. `ACME-12` | `explicit`, or `env` when `issue_ref_source` is `env` | explicit |
| 2 | The issue of a Specivo MCP agent session that overlaps `[started_at, ended_at]` | `mcp_session` | inferred |
| 3 | `attribution.commits` linked to an issue in Specivo | `commit` | inferred |
| 4 | An issue key in `attribution.git_branch`, e.g. `feat/acme-12-login` | `branch` | inferred |
| 5a | `attribution.project_key`, e.g. `ACME` (project only) | `explicit` | explicit |
| 5b | `attribution.repo` matched to a repository connected to a project | `repo` | inferred |
| 6 | Nothing matched: the entry is unassigned | `none` | |

Details of each step:

1. `issue_ref` is the issue the person named, typically from the `SPECIVO_ISSUE` environment
   variable (send `"issue_ref_source": "env"` then). Upper or lower case both work.
2. Agent sessions are opened when an agent uses Specivo's MCP server; the session records the
   issue the agent is working on. The step needs at least one of `started_at` and `ended_at`
   (one alone is a window of one instant). When several sessions overlap, the one sharing the
   longest part of the window wins.
3. `commits` are full SHAs (40 or 64 hex digits) of commits made during the session. The issue
   most of them are linked to wins (links come from commit messages and branch names pushed
   to a connected repository). A tie goes to the older issue.
4. The branch may contain an issue key anywhere as its own token: `ACME-12`,
   `feat/acme-12-login`, `ACME-12/login`. Only keys of projects the reporting account can see
   count.
5. `project_key` attributes the record to a project without an issue. `repo` may be
   `github.com/acme/app`, a clone URL (`https://...`, `git@host:acme/app.git`) or just
   `acme/app`; the host is ignored and the path is compared with the repositories connected in
   Specivo. If the repository is connected to more than one project the target user can log
   to, the record stays unassigned with the warning `attribution_ambiguous`.

**Explicit versus inferred.** The person chose explicit hints, so the server never ignores
them:

- An explicit `issue_ref` or `project_key` that does not exist, that the reporting account
  cannot see, or where the usage cannot be logged **rejects the record** (`not_found`,
  `forbidden`, `target_not_member`). An `issue_ref` outside the `project_key` sent with it is
  `attribution_invalid`.
- An inferred issue or project where the target user cannot log is **dropped**: the record
  gets the warning `attribution_dropped`, and the next step is tried.
- When `project_key` is sent without `issue_ref`, steps 2 to 4 only accept issues of that
  project; others are dropped with `attribution_dropped`.
- A warning never names an issue the reporting account cannot see.

**Replays.** When the bucket already exists and is attributed (by an earlier send or by a
person in Specivo), its attribution stays:

- A record without attribution hints keeps it.
- An explicit hint naming another project or issue **rejects the record** with
  `attribution_conflict`. Send it without attribution, or move the entry in Specivo.
- An inferred result that differs is not applied: the record is stored with the warning
  `attribution_dropped`.
- An unassigned entry takes the attribution of a later send.

## The response

A batch whose envelope is valid always gets **HTTP 200**, even if every record was rejected.
The body (see `result.schema.json`):

```json
{
  "schema": "specivo.ai-usage.v1",
  "server_time": "2026-11-06T12:00:00Z",
  "summary": {"received": 2, "created": 1, "updated": 0, "unchanged": 0, "rejected": 1},
  "results": [
    {
      "index": 0,
      "idempotency_key": "752cc2...",
      "status": "created",
      "entry_id": 1042,
      "revision": 1,
      "attribution": {"method": "branch", "project_key": "ACME", "issue_ref": "ACME-12"},
      "warnings": [],
      "error": null
    },
    {
      "index": 1,
      "idempotency_key": "cd754f...",
      "status": "rejected",
      "entry_id": null,
      "revision": null,
      "attribution": null,
      "warnings": [],
      "error": {
        "code": "invalid_record",
        "message": "started_at must fall on usage_date (a UTC day)",
        "field": "started_at"
      }
    }
  ],
  "warnings": []
}
```

`results` has one item per record, in the order sent; `index` counts from 0. `attribution` is
where the entry is attributed **now**, after this send. `issue_ref` is `null` when the
reporting account cannot see the issue.

**Statuses:** `created`, `updated`, `unchanged`, `rejected`. Each record is stored or rejected
on its own: a rejected record never affects the others.

**Error codes** (`error.code` of a rejected record):

| Code | Meaning | Retry? |
|---|---|---|
| `invalid_record` | The record breaks the schema or a rule on this page; `field` names the property | No, fix it |
| `currency_unknown` | `reported_cost.currency` is not a currency the server knows | No |
| `target_unknown` | No account has the `on_behalf_of` login (told only to accounts that may log for others in the record's project) | No |
| `target_invalid` | The target account is inactive or a system account (same) | No |
| `target_not_member` | The target user is not an active member of the project (same) | No |
| `forbidden` | The reporting account may not log this usage there; for `on_behalf_of` also any of the three above when the account may not tell them apart | No |
| `not_found` | An explicit `issue_ref` or `project_key` does not exist or cannot be seen | No |
| `attribution_invalid` | `issue_ref` is not in `project_key` | No |
| `attribution_conflict` | The stored entry is attributed to something else | Send without attribution |
| `key_conflict` | Another account reported this key for this user | No; use another key |
| `duplicate_key` | The batch already holds this key for this user | No |
| `internal_error` | The server could not store the record | Yes, later |

**Warning codes** (`warnings[].code`; the record was still stored):

| Code | Meaning |
|---|---|
| `attribution_dropped` | An inferred issue or project was not used (see [Issue attribution](#issue-attribution)) |
| `attribution_ambiguous` | The repository matches several projects; the record stays unassigned |
| `unknown_fields` | The record held properties this server does not know; they were ignored |

The batch-level `warnings` may hold `unknown_fields` for unknown top-level properties.

A client must treat a code it does not know like the generic case: an unknown error code means
the record was rejected; an unknown warning code means it was stored.

**Whole-batch errors** use Specivo's usual error body,
`{"errors": [{"code": ..., "message": ..., "field": ..., "details": ...}]}`:

| HTTP | Code | When | Retry? |
|---|---|---|---|
| 400 | `invalid_json` | The body is not JSON | No |
| 400 | `invalid_batch` | The envelope breaks `batch.schema.json` (missing `client`, empty `records`, ...) | No |
| 400 | `unsupported_schema` | `schema` names a version the server does not accept | No; check `GET` |
| 401 | `unauthorized`, `api_key_invalid`, ... | No or a bad key | No |
| 413 | `batch_too_large` | Over 1 MiB, or over 500 records | Split the batch |
| 429 | `rate_limit_exceeded` | Too many batches from this account | After `Retry-After` seconds |
| 5xx | | Server trouble | Yes, with backoff |

## Limits

- At most **500 records** and **1 MiB** (1,048,576 bytes) per request. `GET` returns both.
- A server may limit how many batches an account sends; Specivo allows 60 per minute by
  default (`AI_USAGE_INGEST_RATE_LIMIT_PER_MINUTE`). Wait for `Retry-After` seconds after a
  429.
- Sanity bounds: at most 10^12 tokens per class, 10^8 requests per record; the string lengths
  in the schema. The bound per class applies to each bucket and to the sum of both
  (`tokens.<class> + long_context_tokens.<class>`).

## Compatibility rules

- **Within v1, changes are additive.** New optional properties may appear in records, in the
  batch and in responses. Nothing required is added, nothing is removed, and no meaning
  changes.
- **Clarifications** within v1 make explicit what the contract always meant and how Specivo
  has always processed it; they do not change the format. One so far: `tokens` and
  `long_context_tokens` are disjoint buckets whose sum is the usage (see
  [Long-context tokens](#long-context-tokens)). Earlier wording called `long_context_tokens`
  "the part of `tokens`" billed at long-context prices; a server has always priced the two
  buckets separately, so a reporter that counted long-context requests in both was charged
  twice for them.
- A **server ignores record and batch properties it does not know**, and says so with the
  warning `unknown_fields`. A newer reporter therefore works with an older server; the
  published schemas stay strict (`additionalProperties: false`) so that reporters can catch
  their own typos in tests.
- **Clients ignore response properties they do not know**, and treat unknown codes as
  described in [The response](#the-response).
- A **breaking change** gets a new version, `specivo.ai-usage.v2`, on the **same endpoint**. The
  batch's `schema` property selects the version.
- A server supports the **current and the previous major version**. `GET` lists them.
- The JSON Schemas are versioned with the contract: a reporter should vendor a pinned copy of
  this folder and validate its own output against it.

## Examples

[`examples/valid/`](examples/valid/) holds batches a server accepts and
[`examples/invalid/`](examples/invalid/) batches or records it refuses.
[`examples/expected.json`](examples/expected.json) gives the answer Specivo returns for each,
when they are sent on 2026-11-06 by an account `reporter-bot` on a server holding:

- a project `ACME` with issues `ACME-3`, `ACME-7` and `ACME-12`, where `reporter-bot` may log AI
  usage for itself and for others, and where `teammate` is an active member;
- the repository `acme/app` connected to `ACME`, with the commit
  `32fe3796ff2245cb43d05a41280745a53a12bd54` linked to `ACME-3`.

Specivo's test suite sends every example to its endpoint and checks these answers, then sends
the valid ones again and checks that every record is `unchanged`.

## Open decisions

- **Agent sessions of an on-behalf record.** Step 2 first looks at the agent sessions of the
  reporting account, then at those of the target user. The reasoning: a shared reporter that
  also runs the agent knows best what it worked on. The alternative is to look only at the
  target user's sessions. To be confirmed.
