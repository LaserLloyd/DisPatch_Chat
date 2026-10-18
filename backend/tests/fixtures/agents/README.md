# Fixtures for tests/agents

- `malicious_repo/` - a repo whose `.claude/settings.json` has a PreToolUse and SessionStart hook that write `*.marker` files, whose `.mcp.json` starts a server that writes `MCP_RAN.marker`, and whose `CLAUDE.md` is a prompt injection. Nothing here runs by itself: the isolation tests assert that the agent adapters are configured so that none of it would load (RESEARCH.md F4, F6b, R-ISO; gate G17).
- Do not open this folder as a project in an interactive coding agent: the hooks are real.

