<!--
Thanks for contributing. Delete any section that doesn't apply.
If this fixes a security issue, stop — see SECURITY.md and report privately.
-->

## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Why

<!-- The problem, or a link to the issue. If it's a bug, what was the cause? -->

Fixes #

## How it was tested

<!-- Not "tests pass" — what did you actually do to convince yourself? -->

- [ ] `cd backend && uv run pytest -q`
- [ ] `python3 scripts/scrub_check.py` — no private data
- [ ] `python3 scripts/check-locales.py` — if any string changed
- [ ] Drove the change in a browser (say which, and mobile or desktop)

## Checklist

- [ ] Comments explain **why**, not what — the reasoning, the alternative, the
      invariant being defended
- [ ] New user-facing strings go through `t()` and exist in `locales/en.json`
- [ ] Frontend asset changed → bumped its `?v=` **and** `CACHE` in `sw.js`
- [ ] New endpoint inherits the access gate; new WebSocket route authenticates
      inline (HTTP middleware does not run for the WebSocket scope)
- [ ] No new runtime dependencies, or explained below why one is needed

## Anything reviewers should know

<!--
Trade-offs you made, things you were unsure about, parts you'd like a second
opinion on. "I wasn't sure whether X" is genuinely useful and saves a round trip.
-->
