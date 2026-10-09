# Advisor bots

An **advisor** is a bot that answers straight from a model API, streams its
reply token by token, knows you from a folder of Markdown notes, and can hand
work to your agents. It is meant for the "quick, informed chat partner" role:
advice, planning, thinking out loud, with a way to say "go and find out" or
"go and do it" without leaving the conversation.

An advisor is a normal bot whose entry in `config.yaml` carries an `advisor:`
block. Without the block nothing changes; the block is omitted from the file
when unset.

## When to use which kind of bot

| You want | Use |
|---|---|
| A chat partner backed by one cloud or local model, nothing more | **Connect an AI** bot (`api:` block, see [llm-providers.md](llm-providers.md)) |
| The full agent runtime: tools, files, shell, long jobs, the agent's own memory | An **agent bot** (see [agents.md](agents.md)) |
| A fast chat partner that knows you, falls back across providers, remembers its conversations and can delegate to agents | An **advisor** |

An advisor is not a replacement for an agent. It never runs tools itself; when
something needs doing it asks an agent, and you decide whether the agent goes.

## What it does

- **Real streaming.** Provider deltas are painted as they arrive, so the first
  words show in a second or two. (Plain "Connect an AI" bots fetch the reply
  whole.) `<think>…</think>` blocks are hidden from the stream.
- **A fallback chain.** `providers` is an ordered list, typically a cloud model
  first and a local one second. A provider that fails before its first word
  (bad key, unreachable, HTTP error, or a reply that is only reasoning) falls
  through to the next one. A provider that fails *mid-reply* ends the turn with
  an error rather than splicing two models together.
- **Knowledge.** Each turn carries `_profile.md` (in the system prompt) plus
  the corpus passages most relevant to the conversation. The passages travel
  in front of your message inside a `<reference-data>` block that tells the
  model they are reference data, not instructions (see [Corpus](#corpus)).
- **Delegation.** Inline markers hand work to agents (see [Markers](#markers)).
- **Memory.** A nightly digest turns the day's chats into a note in the corpus.

## Configuration

```yaml
bots:
  - id: sage
    name: Sage
    safe: false                     # advisors are unlocked-only
    advisor:
      owner: "Sam"
      providers:
        - provider: custom
          base_url: https://api.example.com/v1
          model: big-model
          api_key_env: EXAMPLE_API_KEY
          max_tokens: 4096
          read_timeout_s: 90
          extra_body: {thinking: {type: disabled}}
        - provider: custom
          base_url: http://gpu-box:1234/v1
          model: local-model
      knowledge_dir: /srv/dispatch/knowledge
      profile_fallback: [/srv/dispatch/notes/USER.md]
      embeddings:
        base_url: http://gpu-box:1234/v1
        model: embed-model
        timeout_s: 2
      research_agent: worker
      action_agent: lead
      agents: [worker, lead]
      dispatch_timeout_s: 1800
      retrieval_chars: 6000
      history_chars: 24000
```

Only `providers` is required. DisPatch re-reads the file when it changes.

### Keys

| Key | Default | Meaning |
|---|---|---|
| `owner` | `the user` | How the persona refers to the person it talks to ("Sam"). |
| `persona` | built-in | System prompt that replaces the default. `{name}` (the bot's display name) and `{owner}` are filled in. The delegation rules and the profile are appended after it either way; retrieved notes go in the user turn. |
| `providers` | none (required) | Ordered list. Each entry takes the same keys as a bot's `api:` block (`provider`, `base_url`, `model`, `api_key_env` or `api_key`; see [llm-providers.md](llm-providers.md)) plus the extras below. |
| `knowledge_dir` | none | Corpus directory. Without it the advisor has no notes, no saved reports and no digest. |
| `profile_fallback` | `[]` | Files read and used as the profile when `_profile.md` does not exist. |
| `embeddings` | none | Optional vector search. Without it, search is keyword-only. |
| `research_agent` | none | Default agent for `[[research:…]]`; also added to `agents`. |
| `action_agent` | none | Default agent for `[[handoff:…]]`; also added to `agents`. |
| `agents` | `[]` | Allow-list of agent ids a marker may name. With an empty list the advisor cannot delegate at all. |
| `dispatch_timeout_s` | `1800` | How long an agent gets to answer a request. |
| `retrieval_chars` | `6000` | Budget for retrieved passages per turn. |
| `history_chars` | `24000` | Budget for conversation history sent per turn. |

Agent ids are matched case-insensitively. A marker naming an agent outside the
allow-list is redirected to the default for its kind.

### Provider extras

| Key | Default | Meaning |
|---|---|---|
| `max_tokens` | provider default | Sent as `max_tokens`. |
| `extra_body` | none | Merged into the request body verbatim. Use it to switch thinking off where the provider supports that, for example `{thinking: {type: disabled}}`. A reply that contains only reasoning counts as a failure and falls through to the next provider. |
| `read_timeout_s` | `60` | Seconds to wait between streamed chunks. |

### Embeddings keys

| Key | Default | Meaning |
|---|---|---|
| `base_url` | none | Any OpenAI-compatible server exposing `/embeddings`. |
| `model` | none | Embedding model id. Both of these are needed or embeddings stay off. |
| `api_key_env` / `api_key` | none | Credentials, same rules as providers. |
| `timeout_s` | `2` | Budget for embedding the query. On timeout the search is keyword-only, never an error. |
| `extra_body` | none | Merged into the embeddings request. |

## Corpus

The corpus is a directory of Markdown files. **The files are the truth**; the
index is a cache you can delete.

```
knowledge/
  _profile.md                       who the owner is, preferences, priorities
  research/2026-10-09-some-topic.md reports from [[research:…]]
  actions/2026-10-09-some-task.md   reports from [[handoff:…]]
  conversations/2026-10-08-sage.md  nightly digests
  anything-else.md                  notes dropped in by hand or by agents
```

- `_profile.md` is always in the prompt. If it is absent, the `profile_fallback`
  files are used instead.
- Report files carry front matter (`title`, `question`, `agent`, `request_id`,
  `created`). Existing files are never overwritten; a name collision gets a
  numeric suffix. (The nightly digest is the one exception: it is one file per
  advisor per day, replaced in place when re-run with `force`.)
- Files over 2 MB are skipped. Anything else in the directory that is Markdown
  is searchable. Symlinks (files and directories) are never followed, and a
  file whose real path is outside `knowledge_dir` is ignored, so a link to a
  secret cannot be pulled into a prompt.
- The index lives in the data directory as `knowledge-<hash>.db` (SQLite FTS5
  plus embedding vectors). It rescans every two minutes and immediately after a
  report lands. Vectors are filled in the background: a corpus is searchable
  the moment it is scanned and gets better as vectors arrive.
- Search is **hybrid**: keyword ranking and, when the query can be embedded in
  time, cosine similarity, merged by reciprocal-rank fusion.

## Markers

The model writes a marker on its own line; DisPatch strips it from the visible
text and acts on it. At most three markers are acted on per reply.

- A brief may contain `]` (`[[research:worker|compare [1] and [2]]]`); the
  marker ends at the first `]]` not followed by another `]`.
- A reply cut off in the middle of a marker persists without it, and the half
  marker is not acted on.
- Reasoning is hidden whether the model wraps it in `<think>…</think>` or only
  emits the closing `</think>`.
- **Markers only come from the advisor's own reply.** Every piece of text the
  advisor did not write itself (corpus notes, the profile, earlier messages,
  agent reports, model-written briefs echoed into notices) has its directives
  defused before it is used: `[[` becomes `[ [` and `:react:` becomes
  `: react:`. That covers `[[research:…]]`, `[[handoff:…]]`, `[[pic:…]]`,
  `[[media:…]]`, `[[doc:…]]` and reactions alike. A marker whose brief
  appears word for word in the retrieved notes, the profile, earlier
  non-owner messages or an open request is treated as an echo and ignored
  (briefs shorter than 12 characters are exempt from this check).

| Marker | Effect |
|---|---|
| `[[research:<agent>\|<brief>]]` | Dispatched at once. A muted notice appears in the thread; when the agent answers, its report is saved to `research/` and a short "back with an answer" message is posted. |
| `[[handoff:<agent>\|<task>]]` | Shows a card with the task, **Send** and **Dismiss**. Nothing runs until Send. The report is saved to `actions/` and posted. |

Agents run as `agent:<id>:advisor-<request id>` sessions, separate from any
other conversation the agent has. At most three requests run against agents
at once; the rest wait their turn.

What the agent receives: the brief and a short excerpt of the conversation,
each in a fenced block labelled as model-written / context data. A research
request explicitly asks for research only (find out, change nothing); a
handoff carries the task as approved, still under the agent's own rules for
anything that publishes, deploys, spends money or contacts people.

## Request lifecycle

Every marker creates a row in the `requests` table of `advisor.db` in the data
directory.

```
proposed -> running -> done
   |           \-----> failed
   \-> dismissed
```

- `research` requests start at `running`; `handoff` requests start at
  `proposed`.
- Send moves `proposed` to `running`; Dismiss moves it to `dismissed`. Either
  works once: a second press answers "already <state>" with the state the
  request is in now.
- If Send finds the advisor gone or misconfigured, the request is marked
  `failed`, the card is re-stamped, and the press answers 409 saying so.
- An agent that errors, times out or returns nothing sets `failed`, re-stamps
  the card and posts a warning line in the thread.
- If acting on a marker fails after the reply was delivered, the thread gets a
  warning line; the turn itself is not marked as an error.
- **Restart behaviour.** Requests that were `running` when the server stopped
  are marked `failed` ("interrupted by a restart"), their cards are
  re-stamped, and the thread gets a warning telling you to ask again.
  `proposed` cards survive a restart and can still be sent.
- Every request message (the "asked" notice, the card, "sent", the report, a
  failure) is delivered with its own identity (`advisor:<request id>:<phase>`),
  so two similar-looking notices for different requests never swallow each
  other, and none of them ever takes over the bubble of a reply that is still
  streaming.

## Nightly digest

```
POST /api/advisor/digest?day=YYYY-MM-DD[&force=true]
```

Summarises each advisor's chats for that day (default: yesterday) into
`conversations/<date>-<bot>.md` (the bot id slugified: lowercase, with runs of
other characters turned into `-`, so `My_Adv` becomes `my-adv`), using the
first provider that answers. Without `force` it skips days that already have
a file; with `force` it replaces that file in place. The response lists a
result per advisor (`path`, or `skipped` / `error`). `force` is honoured only
for a full unlocked session; a machine caller (cron, agent) always gets the
idempotent behaviour.

Cron example, 04:30 daily:

```
30 4 * * * curl -fsS -X POST "http://127.0.0.1:8765/api/advisor/digest" >/dev/null
```

From the same machine no key is needed; from elsewhere send the API token
(see [security.md](security.md)).

## Status

`GET /api/advisor/status` returns, per advisor, the provider chain (as
`provider/model`, no keys), the agent allow-list, index statistics (file,
chunk and vector counts; not the corpus path) and the 20 most recent requests
(id, kind, agent, brief, state, time, result path, error; never the
conversation excerpt).

## Security model

- **Unlocked only.** Advisors are always `safe: false`: a `safe: true` in
  `config.yaml` or the Bot Manager's Safe toggle is overridden for any bot with
  an `advisor:` block. Safe Mode never sees them, and a turn or a marker on an
  advisor somehow flagged safe is refused.
- **Send needs a real session.** The Send and Dismiss routes require a full
  unlocked browser session. The machine surface (loopback or API token) cannot
  call them, so an agent can never approve its own handoff.
- **A card cannot be forged.** Send and Dismiss carry the id of the message
  the button is on (`{"message_id": …}`), and the server refuses unless it is
  the card it recorded for that request. The card shows the brief from its
  server-stamped metadata, not from the message body. Inbound writes
  (`POST /api/inject`, `POST /api/threads/<id>/messages`) drop any `advisor_*`
  metadata key, so an agent cannot post a message that looks like a card.
- **Research is read-only by instruction, and automatic by design.** A
  `[[research:…]]` marker dispatches without a click. The research brief tells
  the agent to change nothing; anything that needs a change has to come back
  as a recommendation and go through a handoff card.
- **Never a Safe-Mode agent.** An agent id that is a `safe: true` bot (by its
  id, or by that bot's `agent:` routing) is dropped from `agents`,
  `research_agent` and `action_agent` with a log line, and a dispatch to one is
  refused, so a brief and a private excerpt never reach the family tier.
- **The owner profile is data.** `_profile.md` and the open-request briefs go
  into the system prompt inside delimited blocks marked as reference data,
  not instructions; retrieved notes travel in the user turn the same way.
- **The machine surface is status + digest only.** Those two routes are
  machine-inbound so a cron job or watchdog can use them; they refuse
  browser-shaped requests.
- **Keys are redacted** in admin output: provider and embeddings keys come back
  as `has_key` booleans. Prefer `api_key_env` so the key never sits in
  `config.yaml`.
- **The corpus is readable by agents you allow.** A report is whatever the
  agent returned, written to disk. Do not put anything in `knowledge_dir` that
  you would not want an allow-listed agent, or anything that indexes the
  directory, to see.
- Research and handoff briefs include a short excerpt of the recent
  conversation so the agent has context.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "This advisor has no model configured" | `advisor.providers` is empty or has no mappings. |
| Reply is an error naming the first provider | Every provider failed before its first word; the last error is shown. Check the key, URL and model id. |
| "The model replied with nothing" | The provider returned only reasoning. Disable thinking with `extra_body` or let the chain fall through. |
| Reply stops mid-way with an error | A provider dropped after streaming began; DisPatch does not splice models. Ask again. |
| Bot never delegates | `agents` is empty, or the model was not told it can; check `GET /api/advisor/status` shows your agents. |
| Notes not found | Check `knowledge_dir`, then the status endpoint's index stats. Search degrades to keywords if embeddings time out; that is normal. |
| Card says "already running" | The request was sent or restarted by an earlier press. |
| No digest file | The day had no advisor messages, the file exists (use `force=true`), or the advisor has no `knowledge_dir`. |
