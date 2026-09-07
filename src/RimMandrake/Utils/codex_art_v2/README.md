# codex_art_v2 — the experimental second graphics pathway

**Status: EXPERIMENTAL. Never run for real. Pending an owner ruling on whether to
keep, promote or discard it.** The verdict and the reasoning are in
`infrastructure/agents/OPUS_REVIEW_codex_graphics_second_pipeline.md`.

**Pathway 1 — `skills/generating-images/` — is the live, working pipeline and is
UNCHANGED.** Nothing here is imported by it, referenced from it, or able to
affect it. This package imports *from* it (read-only) so the facts about
locating `codex.exe` live in one place.

This is deliberately **a small fraction** of
`infrastructure/agents/CODEX_PROPOSAL_ART_WORKER.md`. The durable job queue,
claim protocol, control inbox, event log, manifest contract, worker
self-iteration and selection authority were judged not worth building for this
project; the review doc says why for each.

## What runs today

```bash
# Is the app-server transport reachable? Spends NO quota.
python3 src/RimMandrake/Utils/codex_art_v2/cli.py probe

# Live account limits + a batch verdict. Spends NO quota.
# Useful TODAY, to the EXISTING pipeline: run it before a batch driver.
python3 src/RimMandrake/Utils/codex_art_v2/cli.py usage

# Everything, against a fake app-server. No real Codex, no quota.
python3 src/RimMandrake/Utils/codex_art_v2/selftest.py
```

`cli.py generate` refuses to run without `--owner-authorized "<verbatim words>"`.
A real turn spends the owner's Codex quota and no agent can authorize that.

## Files

| file | what it is |
|---|---|
| `appserver.py` | JSON-RPC client for `codex app-server` over stdio |
| `scheduler.py` | `account/rateLimits/read` → a batch verdict. Pure functions |
| `cli.py` | `probe` / `usage` / `generate` (gated) |
| `fake_appserver.py` | mock server for tests — never calls OpenAI |
| `selftest.py` | 41 assertions, all against the fake |

## Verified facts (codex-cli 0.153.1, 2026-09-06)

- `codex app-server` exists and works from WSL, but `codex --help` flags it
  **[experimental]**. An app update can move it. `probe` is how you find out.
- Every method the proposal names exists verbatim: `initialize`, `thread/start`,
  `thread/resume`, `thread/compact/start`, `thread/archive`, `turn/start`,
  `turn/steer`, `turn/interrupt`, `account/rateLimits/read`, `skills/list`.
- **Responses and notifications omit the `"jsonrpc"` field.** A client that
  filters on it sees nothing. Demux on `"id"` vs `"method"`.
- The server volunteers a notification immediately after `initialize`.
- Regenerate the protocol schema to check any of this:
  `codex app-server generate-json-schema --out <dir>`.
- `auth_mode` is still `chatgpt` with `OPENAI_API_KEY: null`, so the API-key-only
  routes remain shut. `CODEX_PROPOSAL_GENERATING_IMAGES_SKILL_DRAFT.md`'s claim
  that this limitation "is stale" was not true when measured on 2026-09-06.

## Rules this package keeps

- **It writes nothing to shared machine state.** `queue_root()` resolves the path
  the proposal names (honouring `RIMWORLD_CODEX_ART_QUEUE`) and never creates it.
  A selftest asserts this.
- **It kills only PIDs it owns.** Never `pkill -f codex` — that pattern matches
  this script's own argv and the owner's desktop Codex app.
- **A timeout is not a verdict.** `wait_for_turn` returns
  `timeout_outcome_unknown`, never "failed", because an image may exist under
  `$CODEX_HOME/generated_images`. Treating a timeout as failure is the live
  wrapper's filed defect (`CODEX_WRAPPER_HARVEST_FIX_1`, ~14 orphaned images).
