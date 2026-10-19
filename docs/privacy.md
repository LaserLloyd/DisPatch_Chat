# Privacy: what leaves this machine

DisPatch keeps your chats, files and settings on the machine it runs on. The places your words
can go elsewhere are the ones you connect on purpose: a model provider for a character, and an
agent backend for a task. This page says, for agents, exactly what goes where, what DisPatch
stores about it, and who can see it. (The wider data inventory lives in
[security.md](security.md).)

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
| Restricted profile | nothing: no agents, no tasks, no frames, no notices (a Restricted profile can never be granted the permissions) |
| Member (granted `dispatch_agents`) | only the agents published to them and only their own tasks |
| Admin, Owner | every task, including its prompt and timeline, and the audit log |

### Notifications

A Web Push ping about a task says only "A task needs your approval", "A task finished" or "A
task failed" - no agent name, no text. The matching notice line appears in the chat the task was
started from, visible to whoever can see that chat. Pushes go to remembered devices; they are not
yet routed per person.
