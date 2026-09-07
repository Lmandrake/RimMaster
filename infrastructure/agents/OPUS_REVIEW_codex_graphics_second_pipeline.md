# Review: is the CODEX graphics architecture worth building?

Reviewer: Claude (Opus), 2026-09-06. Commissioned by the owner: *"see if they are
different than how we currently generate graphics. If there is new wisdom or a
path forward, let's implement the 2nd pathway explicitly for testing vs. the
current pipeline. Do not harm the current CODEX pipeline, since it works."*

Subject: `CODEX_PROPOSAL_ART_WORKER.md`, `CODEX_PROPOSAL_GRAPHICS_WORKFLOW.md`,
`CODEX_PROPOSAL_GENERATING_IMAGES_SKILL_DRAFT.md` — all authored by Codex, none
ruled, none implemented.

---

## Verdict in one line

**The transport is real and one idea in it is worth stealing today; the
architecture around that idea is not warranted for this project. I built roughly
15% of the proposal — the app-server client and the usage scheduler — and
deliberately did not build the queue, the worker contract, or the self-iteration
loop.**

The live pipeline is untouched: `git diff skills/` is empty.

---

## What I verified rather than relayed

The proposal is **not hallucinated**, and it deserves credit for that. I checked
its central factual claims against the installed binary (codex-cli **0.153.1** —
the contract doc's 0.147.0-alpha.6.6 is stale on version, though its auth facts
still hold):

- I dumped the protocol's own schema with
  `codex app-server generate-json-schema --out <dir>` (39 files, 622 definitions).
  **Every JSON-RPC method the proposal names exists verbatim**: `initialize`,
  `thread/start`, `thread/resume`, `thread/compact/start`, `thread/archive`,
  `turn/start`, `turn/steer`, `turn/interrupt`, `account/rateLimits/read`,
  `skills/list`. Nothing was invented.
- I completed a **real handshake from WSL** — `initialize`, then
  `account/rateLimits/read` — and got live account state back. The transport
  works, from this machine, today, at zero quota cost.

So the honest starting position is: this design is buildable. The question is
whether it *should* be built, and that is a different question, which the
proposal never asks about itself.

**One factual defect found.** The draft skill states the one-shot pipeline's
"old transparency limitation is stale." Measured 2026-09-06: `auth_mode` is
still `chatgpt` and `OPENAI_API_KEY` is still `null`, so the API-key-only routes
(exact `--size`, `--mask`, native `--background transparent`) remain shut. The
draft reversed a measured fact without measuring. *(Separately and genuinely:
`LESSONS_INBOX.md` records that `rembg_cut.py` has replaced chroma-key for the
local cutout step — a real change the live `SKILL.md` has also not caught up
with. That is a different claim from the one the draft makes.)*

---

## Does this project have the problems the architecture solves?

I got ground truth on how art is actually made here rather than accepting the
proposal's framing.

**Scale.** Thirteen scripts call `codex_image.py`; three are batch drivers
(`gen_livestock_mockups.py` = 9 images, `gen_sea_mockups.py`, and
`gen_sea_facings.py` = creatures × 2 facings). Real runs are **single digits to
low tens of images**. These are not long-lived services; they are Python loops an
agent wrote minutes earlier for one asset family and will not run again.

**Parallelism — already solved, and cheaper.** `gen_sea_facings.py` already runs
`ThreadPoolExecutor(max_workers=3)`. `LESSONS_INBOX.md` records four concurrent
`codex exec` sessions measured at 102 img/h against ~58 serial, and names the
actual requirement: *each worker needs its own `CODEX_HOME`*. That is a two-line
environment change. `CODEX_PARALLEL_WORKERS_1` is already filed for it. The
proposal's atomic-rename claim protocol arbitrates contention between separate
worker *processes* that this project does not run — its parallelism lives inside
one process, where the correct primitive is a lock, not a filesystem rename.

**Durable queue — solves a real symptom, wrong cause.** Work genuinely does
strand across sessions: `GOD_ART_LOCAL_HARDWARE_PARKED_1` freezes an inventory of
3 uncut and 25 never-started images; `CODEX_WRAPPER_HARVEST_FIX_1` records ~14
orphaned images. But the *causes* were (a) an owner policy ruling that paused the
track and (b) a bug in the wrapper. A durable queue prevents neither and resumes
neither better than the item file that already did the job. The stranding is not
a scheduling failure.

**The one real, measured, filed failure mode is small.** `CODEX_WRAPPER_HARVEST_FIX_1`:
the wrapper treats *its own timeout* as "no image" and discards work that
completed. The proposal's error table independently identifies this ("Stream
disconnect or outer process timeout | Outcome unknown"). That is genuine wisdom —
and it is a ten-line fix inside `codex_image.py`, not a reason for an app-server.

**Live steering — unmotivated.** A generation returns in 79–93 s. There is no
evidence anywhere in the repo of anyone wanting to correct a prompt at t=40 s.
The one real "stop" event on record is the owner halting three background
*agents* mid-AFK; they were killed by PID. `turn/interrupt` would not have
touched them, because the thing that needed stopping was the Python retry loop,
not a Codex turn.

**Self-iteration and `selection_authority: worker` — an active regression.** The
live skill's thesis is that Codex is the worker and *"the controller is the half
that can look at the result and iterate."* The sprites skill adds an offline
validator, contact sheets and a REJECT/WARN grade; the owner reviews art by
looking, increasingly as savegames and review sheets. Letting Codex pick the
winner inserts a judgment that this project's whole doctrine exists to keep in
Claude's and the owner's hands. It is not a feature here; it is work to undo.

**Spec-to-evidence ratio.** 1,264 lines of contract — event sequencing,
`error.interpretation` distinct from `error.message`, eight terminal states,
checkpoint-recovery turns — for a worker that has never run once. The proposal
says so itself: *"executable controller not yet implemented."* Every number in it
is a design intention, not a measurement. Against that, the thing it would
replace has a measured 8-of-9 success rate and a documented history of **three
published wrong diagnoses** about its own failures, each corrected by running the
simplest version of the call by hand. That history is the project's real
methodology, and the proposal does not participate in it.

**And it adds surface.** `codex --help` flags `app-server` **[experimental]**.
The one-shot pipeline rides `codex exec`, a stable documented subcommand. Trading
a stable transport for an experimental one, to gain features nobody has needed,
on a project whose recurring pain is *tools that report success and change
nothing*, is the wrong direction.

---

## What I built, and what I refused to build

Everything lives in **`src/RimMandrake/Utils/codex_art_v2/`** — dev tooling, not
a skill, so it is not auto-discoverable and cannot be invoked by mistake. Its
selftest *is* picked up by `run_selftests.py`, deliberately: experimental code
that nobody checks rots.

### Built

| file | why it earned its place |
|---|---|
| `appserver.py` | The transport, proven reachable. Feature-detects; kills only PIDs it owns; reports a timeout as `timeout_outcome_unknown`, never "failed" |
| `scheduler.py` | **The load-bearing steal.** `account/rateLimits/read` → a batch verdict, with Pacific reset times |
| `cli.py` | `probe` / `usage` / `generate` (gated behind `--owner-authorized`) |
| `fake_appserver.py` | Mock server reproducing the real one's quirks. Never calls OpenAI |
| `selftest.py` | 41 assertions, **41/41 passing**, all against the fake |

**Why the scheduler is worth having on its own merits.** `LESSONS_INBOX.md` says
*"the weekly token budget, not per-minute image rate, is the binding limit."*
That number is free to read, and **nothing in the current pipeline reads it**. A
batch driver will launch twenty generations into a spent weekly window and
discover it by failing partway through — with errors that look exactly like the
flaky-generation failures the skill tells you to just retry. Run live while
writing this:

```
plan: plus
5-hour      23% used /  77% left  (window 5h)    resets 2026-09-06 11:16:31 PM PDT
weekly      82% used /  18% left  (window 168h)  resets 2026-09-06 08:05:28 PM PDT

verdict: DISPATCH up to 1 worker(s), max 2 iteration(s) per job
```

At 82% weekly, the correct move right now is one worker, not the three
`gen_sea_facings.py` would use. The current pipeline cannot know that. This is
the proposal's genuine contribution and it needs none of the rest of it.

### Refused, with reasons

| not built | why |
|---|---|
| Durable queue (`pending`/`claimed`/`accepted`/`needs_review`/`needs_control`/`failed`/`refused`/`stopped`) | Arbitrates between worker processes this project does not run; does not address why work actually stranded |
| Atomic-rename claim protocol | Contention here is in-process — a lock, not a rename |
| `events.jsonl` + monotonic sequencing + manifest contract | Audit machinery for a system nobody audits. The evidence trail here is contact sheets and savegames |
| Control inbox (`feedback`/`amend`/`pause`/`resume`/`accept`/`reject`) | No recorded instance of wanting any of them |
| Worker self-iteration and `selection_authority: worker` | Moves art judgment away from the owner and Claude — backwards for this project |
| `turn/steer` mid-generation | Wired in `appserver.py` because it costs nothing to expose; no caller, because no need |
| Writing anything to `C:\Users\Mandrake\AppData\Local\RimworldCodexArtQueue` | Shared machine state. `queue_root()` resolves the path and honours `RIMWORLD_CODEX_ART_QUEUE`; a selftest asserts nothing is created |

---

## What still needs the owner's authorization

1. **The one real smoke test — NOT DONE, and I did not have authority to do it.**
   No image was generated during this review and no image quota was spent. The
   proposal's own checklist gates this on owner authorization. `cli.py generate`
   refuses to run without `--owner-authorized "<verbatim words>"`.
2. **Keep, promote, or discard `codex_art_v2/`.** It is inert until ruled on.
3. **Whether `usage` should be adopted into the live skill.** I recommend it —
   one line telling a batch driver to check limits first. **I did not make that
   edit**, because it changes pathway 1 and the instruction was not to touch it.
4. **Whether to close the loop on the cheap fixes instead.** If only one thing
   happens next, it should be `CODEX_WRAPPER_HARVEST_FIX_1` (~14 orphaned images,
   already filed), then per-worker `CODEX_HOME` under `CODEX_PARALLEL_WORKERS_1`.
   Between them they capture most of the proposal's real value at a fraction of
   the cost.

---

## Pathway 1 vs pathway 2, once both exist

Reach for **pathway 1** (`skills/generating-images/`, one-shot `codex exec`) for
essentially all art: it is the working, measured route, it rides a stable CLI
subcommand, its failure modes are documented down to which wrong diagnoses were
published before the right one, and its one-generation-per-foreground-call
discipline was learned the hard way. Reach for **pathway 2**
(`src/RimMandrake/Utils/codex_art_v2/`) today only for `usage` — free, fast, and
the only way to see the weekly budget that actually governs whether a batch will
survive — and, if the owner ever authorizes it, as the place to test whether a
persistent thread genuinely improves multi-facing identity consistency, which is
the single claim in the proposal that could justify the rest of it and that
nobody has measured. If that measurement never happens, pathway 2 should shrink
to `scheduler.py` and the rest should be deleted rather than left to rot: an
unproven second pipeline that lingers is how a project ends up with two ways to
do everything and confidence in neither.
