# GitPilot — Instructions for Claude Code

GitPilot is a stateful AI engineering control plane for GitHub. Full architecture
lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the connection/brain/
guided-fix pipeline in [`docs/BRAIN.md`](docs/BRAIN.md) — read those before making
structural changes. Don't restate their content here; this file is about *how to
work in this repo*, not what it does.

## Source of truth

- **Code correctness**: the code itself and `python -m pytest -q`, never a doc.
- **Architecture**: `docs/ARCHITECTURE.md`, `docs/BRAIN.md`.
- **Extension boundary**: `services/contracts.py` — any new tracker, code host,
  memory backend, or AI provider implements these Protocols; don't bypass them.
- **Fix-run state machine**: `services/memory_service.py`'s `RUN_TRANSITIONS`.
  Don't invent new run states outside it.

## Where solved issues and patterns go

This repo has a real, durable memory system — don't create parallel markdown
"brain files" for bug patterns, API quirks, or RCA notes. Use the existing tools:

- `memory_remember(repository, kind, title, content, tags)` — store a solved
  pattern, gotcha, or root cause once it's confirmed (`kind="pattern"` or
  `kind="gotcha"`).
- `memory_recall(repository, query)` — check before re-diagnosing something
  that may already be solved.

This keeps knowledge scoped per-repository, queryable, and auditable in
`run_events` instead of scattered across static files that go stale.

## RCA discipline before proposing a fix

1. `memory_recall` first — has this repository seen this before?
2. `code_search_repository` for the actual current field/route/symbol name —
   never assume a name from a doc or prior session; docs and memory can be stale.
3. Confirm the failure is reproducible (test or explicit repro steps), not a
   one-off hallucinated symptom.
4. Only then propose a patch, and keep it to the smallest verifiable change.
5. After verification succeeds, `memory_remember` the root cause + fix — not
   the whole diff, just what a future session needs to avoid re-diagnosing it.

## Hard boundaries (see `docs/BRAIN.md` §16 for the full list)

- Never touch `.env`, `data/*.db*`, or `data/.gitpilot-master-key` — these are
  gitignored for a reason and must never appear in a commit, log, or screenshot.
- Edits are exact-match only; verification commands are manifest-derived only —
  never invent or run arbitrary shell commands as a "verification."
- A guided-fix run needs its own explicit human approval to edit, and a
  *second, separate* approval to publish. Don't collapse those steps.

## Tests

```cmd
python -m pytest -q
```

Live-credential tests (`tests/test_live_integration.py`,
`tests/test_live_guided_fix_integration.py`) are opt-in and skipped by default.
