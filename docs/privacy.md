# Privacy: what leaves this machine

DisPatch keeps your chats, files and settings on the machine it runs on. The places your words
can go elsewhere are the ones you connect on purpose: a model provider for a character, and an
agent backend for a task. This page says who can see a chat inside the app and, for agents, exactly
what goes where and what DisPatch stores about it. (The wider data inventory lives in
[security.md](security.md).)

## Who can see a chat

**Admins can't open your chats in the app.** A chat belongs to the profile it was had in. Other profiles
can't list, open, search, export or receive live updates of it, and that includes Admins and the
first (protected) Admin. A Basic profile's chats are as private as a User's, a profile's own bots are
private to it, and its files and pictures are its own (the File Server shows each profile only what it
uploaded). Two things are shared on purpose:

- **The DisPatch profile** is the default profile that locked and unsigned devices land on. Everyone
  who uses it sees its bots and chats, and the sign-in popup says so every time.
- **Household bots** are managed by Admins, but each profile's chats with one are its own.

Admins see an activity summary (how much, never what): counts of sign-ins, messages and uploads, and an
audit log of administrative actions that carries ids and counts, never names or content.

What this does **not** promise: the host. Whoever can read the data folder or a backup can read every
chat, an Admin can download a backup, and an **agent bot** runs commands on this computer and can read
anything on it, including anyone's chats. That is why agent bots are off for Users and Basic profiles
by default, never on for the shared DisPatch profile, and why the screen says so plainly when an Admin
switches them on. A chat picture's address (`/media/<uuid>`) is unguessable but is not checked per owner: any
profile that has the address can fetch it, and a deleted picture stays fetchable by anyone who kept
the address until the last message that uses it is gone. OpenClaw's own session records for an agent bot outlive a Reset. Chats are not end-to-end
encrypted. A voice recording is protected by the same ownership rules but is a plain file in
`<data>/voices/`.

A **Reset to default** deletes a profile's bots, chats, pictures and File Server files (the confirmation
names the counts); cloned voices are kept. Anyone using the shared DisPatch profile can reset it.

## Agents and tasks

### What each backend sends where

| Backend | What leaves DisPatch | Where it goes |
|---|---|---|
| **OpenClaw** | the task prompt and file paths | the OpenClaw gateway on this machine (loopback) or its CLI. Whatever model provider that OpenClaw agent is configured with then sees it: that is OpenClaw's setting, not DisPatch's. |
| **Claude Agent** | the prompt, what the agent's tools read (files, web pages, MCP tool output) and its own replies | **Anthropic's API**, or the Amazon Bedrock / Google Cloud / Microsoft Foundry account whose credentials you set up. The working folder, the transcript and the approvals stay on this machine. |
| **Claude Managed Agents** (hosted) | the same, and the session history is *stored* there | **Not built** (milestone M2). When it is added it will be opt-in behind a flag, with this notice on its setup screen: the history lives on Anthropic's platform until you delete the session, the feature is in beta and is not eligible for zero data retention, and DisPatch will offer a "Delete remote session" action. |

The Assign task dialog tells you which of the first two applies before you press Assign.
Use of the Claude Agent is subject to the terms and usage policy of the Anthropic account
whose key you supply; if you run DisPatch for other people you are the operator: tell them an
AI is involved, that its output can be wrong, have a person review anything high-stakes, and
remember that you are responsible for what an agent does with the tools you gave it.

### What DisPatch stores

- A **task row** per task: who started it, which agent, the state, the estimated cost,
  tokens, turns, timings and the chat it was started from. This is the audit record and is kept.
- The **transcript**: the prompt, the event timeline (text, tool calls and results, approvals
  with a short redacted summary of their arguments) and the result text. It is plain text in
  the database. For the Claude Agent, the agent's own session transcript and memory are plain
  JSONL under `<data>/agents/<task>/.claude/`. **Retention** (Settings -> AI & Connections -> Agents, default 30
  days, 0 = keep) deletes the database transcript, the prompt, the result text and that
  folder for finished tasks, and keeps the task row. OpenClaw keeps its own transcripts in the
  agent's home; DisPatch does not manage those.
- A **daily spend ledger** (per agent and per person) behind the budgets. Figures are the
  backend's own client-side estimates, never billing data.
- **Credentials**: the Claude API key and MCP secrets are write-only. They live in the OS
  keyring when one is usable, otherwise in a file readable only by you (owner-only permissions;
  on Windows it relies on the data directory's access rules). They are never returned by the
  API, never put in an event, a log line, a notice or a support bundle, and anything shaped like
  an Anthropic key is masked wherever it would appear.
- **Support bundles** exclude prompts, instructions, results and events by default; the agent
  section is counts and configuration shape only.

### Who can see it

| Who | Sees |
|---|---|
| User or Basic profile with `dispatch_agents` switched on by an Admin | only the agents published to them and only their own tasks |
| User or Basic profile without it (the default), and the shared DisPatch profile (always) | nothing: no agents, no tasks, no frames, no notices |
| Admin | only their own tasks and approvals, plus the aggregate spend (`/api/agents/usage`) and the audit log |

Agent bots can run commands on this computer and read anyone's chats. Switching them on for a profile
is an Admin's decision, and the screen says so.

### Notifications

A Web Push ping about a task says only "A task needs your approval", "A task finished" or "A
task failed" - no agent name, no text. The matching notice line appears in the chat the task was
started from, visible to whoever can see that chat. Pushes go to remembered devices; they are not
yet routed per person.
