# Contracts

Everything in this directory (apart from this file) is **generated** from the code by
`python scripts/gen_contracts.py` and checked by CI (`backend/tests/test_contracts_drift.py`).
Do not edit it by hand: change the code, regenerate, commit both.

| File | What it freezes | Source of truth |
|---|---|---|
| `openapi.json` | The HTTP API (paths, bodies, responses) | the FastAPI app |
| `permissions.json` | Permissions, roles, the role -> permission matrix, machine scopes, old -> new names | `backend/app/contracts/permissions.py` |
| `routes.json` | For every route and WebSocket action: who may call it (permission, machine scopes, `scoped`, `public` + why) | `backend/app/route_policy.py` |
| `errors.json` (+ [`../errors.md`](../errors.md)) | Every error code: HTTP status, message, fix, docs anchor | `backend/app/contracts/error_catalog.json` |
| `capabilities.json` | What DisPatch can do and where: HTTP route, WebSocket action, UI entry point, CLI/MCP (all `null` today) | `backend/app/contracts/capabilities.py` |
| `manifests/*.schema.json` | JSON Schemas for the Connection, Tool plugin, Voice provider and Character manifests (schema only; W5/W6 populate them) | `backend/app/contracts/manifests.py` |

## Rules

- **Stable within a major version.** Paths, permission names, error codes and manifest
  fields are not renamed or repurposed; they are added to. Removing one, or making an
  optional parameter required, is a breaking change and needs a major version bump
  (`scripts/gen_contracts.py:breaking_changes` describes what counts).
- **Branch on `error.code`, never on the message.**
- **Every route declares its permission** in `route_policy.py`; a route without one is
  refused at runtime and fails `tests/test_route_table.py` at build time.
- `capabilities.json` lists `unmapped_routes`: routes no capability claims yet. That is a
  backlog, not an error.

## Using them

```
python scripts/gen_contracts.py           # regenerate
python scripts/gen_contracts.py --check   # CI: exit 1 if the committed copy is stale
```
