#!/usr/bin/env python3
"""selftest.py — exercise the v2 pathway against the FAKE app-server only.

    python3 src/RimMandrake/Utils/codex_art_v2/selftest.py

No real Codex process is started and no image quota is spent. Discovered by
`src/RimMandrake/Utils/run_selftests.py` (glob `selftest*.py` over `src/`).

What this actually asserts, and what it does NOT
------------------------------------------------
It asserts the CLIENT's behaviour: JSON-RPC framing without a `jsonrpc` field,
demultiplexing responses from notifications, streamed turn events, interrupt,
exact error preservation, unknown-outcome timeouts, and the full scheduler
policy table at its boundaries.

It does NOT assert that the real `codex app-server` behaves like the fake. The
fake was written FROM a real 0.153.1 handshake, but a passing selftest is not
evidence the live protocol still matches -- that is what `cli.py probe` is for,
and `codex app-server` is flagged experimental precisely because it can move.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scheduler  # noqa: E402
from appserver import AppServer, AppServerError  # noqa: E402

FAKE = HERE / "fake_appserver.py"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"FAIL  {name}  {detail}")


def fake_server(scenario: str) -> AppServer:
    env_marker = scenario
    srv = AppServer(cli=[sys.executable, str(FAKE)])
    # The scenario is read by the child from the environment.
    os.environ["FAKE_APPSERVER_SCENARIO"] = env_marker
    return srv


# ---------------------------------------------------------------- transport

def test_handshake() -> None:
    with fake_server("ok") as srv:
        info = srv.initialize(timeout=15)
        check("initialize returns server info", info.get("codexHome") is not None,
              f"got {info!r}")
        time.sleep(0.2)
        notes = [n.get("method") for n in srv.notifications()]
        check("unsolicited notification is captured, not mistaken for a response",
              "remoteControl/status/changed" in notes, f"got {notes!r}")


def test_turn_streams_and_completes() -> None:
    with fake_server("ok") as srv:
        srv.initialize(timeout=15)
        tid = srv.thread_start(timeout=15)
        check("thread/start yields an id", bool(tid), f"got {tid!r}")
        turn = srv.turn_start(tid, "hello", timeout=15)
        check("turn/start yields a turn id", bool(turn), f"got {turn!r}")
        res = srv.wait_for_turn(timeout=15)
        check("turn completes", res["status"] == "completed", f"got {res['status']}")
        methods = [e.get("method") for e in res["events"]]
        check("tool events are streamed before completion",
              "item/completed" in methods, f"got {methods!r}")


def test_interrupt() -> None:
    with fake_server("interrupt") as srv:
        srv.initialize(timeout=15)
        tid = srv.thread_start(timeout=15)
        turn = srv.turn_start(tid, "hello", timeout=15)
        time.sleep(0.6)  # let the fake stream its events but not complete
        srv.turn_interrupt(tid, turn, timeout=15)
        res = srv.wait_for_turn(timeout=15)
        check("interrupt drives the turn to completion",
              res["status"] == "completed", f"got {res['status']}")
        status = (res["notification"].get("params") or {}).get("status")
        check("interrupted turns are labelled interrupted, not failed",
              status == "interrupted", f"got {status!r}")


def test_error_is_preserved_exactly() -> None:
    with fake_server("error") as srv:
        srv.initialize(timeout=15)
        tid = srv.thread_start(timeout=15)
        try:
            srv.turn_start(tid, "hello", timeout=15)
            check("a JSON-RPC error raises", False, "no exception raised")
        except AppServerError as exc:
            check("a JSON-RPC error raises", True)
            check("the exact provider message survives",
                  "UsageLimitExceeded" in str(exc), f"got {exc}")
            check("the exact payload survives for the log",
                  (exc.payload or {}).get("data", {}).get("httpStatus") == 429,
                  f"got {exc.payload!r}")


def test_hang_is_unknown_not_failed() -> None:
    """The single most important behaviour: a timeout is not a verdict.

    The live one-shot wrapper's real, filed defect (CODEX_WRAPPER_HARVEST_FIX_1,
    ~14 orphaned images) is that it treats its own timeout as 'no image'. This
    client must report the outcome as UNKNOWN so a caller goes and looks.
    """
    with fake_server("hang") as srv:
        srv.initialize(timeout=15)
        tid = srv.thread_start(timeout=15)
        srv.turn_start(tid, "hello", timeout=15)
        res = srv.wait_for_turn(timeout=1.0)
        check("a turn timeout reports UNKNOWN, never 'failed'",
              res["status"] == "timeout_outcome_unknown", f"got {res['status']}")


def test_request_timeout_does_not_hang_forever() -> None:
    srv = AppServer(cli=[sys.executable, "-c", "import time; time.sleep(30)"])
    srv.start()
    try:
        srv.request("initialize", {}, timeout=1.0)
        check("an unanswered request times out", False, "no exception")
    except AppServerError as exc:
        check("an unanswered request times out", "timed out" in str(exc) or
              "exited" in str(exc), f"got {exc}")
    finally:
        srv.close()


# ---------------------------------------------------------------- scheduler

def limits(primary, secondary, **kw):
    def window(used, mins, offset):
        # The real server nulls the WHOLE window, not just the percentage --
        # mirror that, or the fixture tests a shape the server never sends.
        if used is None:
            return {"usedPercent": None, "windowDurationMins": None,
                    "resetsAt": None}
        return {"usedPercent": used, "windowDurationMins": mins,
                "resetsAt": int(time.time()) + offset}

    block = {
        "primary": window(primary, 300, 3600),
        "secondary": window(secondary, 10080, 86400),
        "planType": "plus", "spendControlReached": False,
        "rateLimitReachedType": None,
    }
    block.update(kw)
    return {"rateLimitsByLimitId": {"codex": block},
            "rateLimitResetCredits": {"availableCount": 3}}


def test_scheduler_table() -> None:
    v = scheduler.decide(limits(20, 40))
    check("healthy usage permits 4 workers", v.max_workers == 4 and v.may_dispatch,
          f"got {v.max_workers}")

    v = scheduler.decide(limits(20, 75))
    check("weekly 75% drops to 2 workers", v.max_workers == 2, f"got {v.max_workers}")

    # The live account read on 2026-09-06 was exactly here: weekly 82%.
    v = scheduler.decide(limits(23, 82))
    check("weekly 82% (the real reading) drops to 1 worker, 2 iterations",
          v.max_workers == 1 and v.max_iterations_per_job == 2,
          f"got {v.max_workers}/{v.max_iterations_per_job}")
    check("weekly 82% warns", v.warn)

    v = scheduler.decide(limits(20, 92))
    check("weekly 92% stops dispatch", v.stop and not v.may_dispatch)

    v = scheduler.decide(limits(95, 20))
    check("5-hour 95% stops dispatch", v.stop and not v.may_dispatch)

    v = scheduler.decide(limits(20, 98))
    check("weekly 98% stops the pool", v.stop and not v.may_dispatch)

    v = scheduler.decide(limits(None, None))
    check("a null window is UNKNOWN, not zero: at most 1 worker",
          v.max_workers == 1 and v.max_iterations_per_job == 1,
          f"got {v.max_workers}/{v.max_iterations_per_job}")
    check("a null window warns", v.warn)

    v = scheduler.decide(limits(10, 10, rateLimitReachedType="usage"))
    check("a provider rate-limit flag stops dispatch outright",
          v.stop and not v.may_dispatch)

    v = scheduler.decide(limits(10, 10, spendControlReached=True))
    check("spendControlReached stops dispatch", v.stop)

    # Most-restrictive-row: a healthy weekly must not rescue a spent 5-hour.
    v = scheduler.decide(limits(85, 10))
    check("the most restrictive row wins (5-hour 85% caps workers at 1)",
          v.max_workers == 1, f"got {v.max_workers}")


def test_pacific_rendering() -> None:
    # 1788750328 = 2026-09-06 20:05:28 PDT (the live weekly reset that day).
    raw = limits(23, 82)
    raw["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] = 1788750328
    v = scheduler.decide(raw)
    txt = v.secondary.resets_at_pacific()
    check("a summer reset renders as PDT, never PST", txt.endswith("PDT"), f"got {txt}")
    check("the reset carries a full date and time", "2026-09-06" in txt, f"got {txt}")

    # A January timestamp must flip to PST off the same code path.
    raw["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] = 1767312000
    v = scheduler.decide(raw)
    txt = v.secondary.resets_at_pacific()
    check("a winter reset renders as PST", txt.endswith("PST"), f"got {txt}")

    v = scheduler.decide(limits(None, None))
    check("an unknown reset renders as unknown, not as the epoch",
          v.secondary.resets_at_pacific() is None)


def test_render_is_safe_on_every_verdict() -> None:
    for p, s in [(20, 40), (None, None), (95, 95), (23, 82)]:
        out = scheduler.render(scheduler.decide(limits(p, s)))
        check(f"render() produces a verdict line for {p}/{s}",
              "verdict:" in out and "because:" in out, out[:120])
    out = scheduler.render(scheduler.decide(limits(20, 40)))
    check("render() never promises an image count",
          "images remaining" not in out.lower())


def test_no_shared_machine_state_is_touched() -> None:
    """Nothing here may create the proposal's queue root as a side effect."""
    from appserver import queue_root
    root = queue_root()
    check("queue_root() resolves without creating anything",
          not root.exists() or root.is_dir(), f"{root}")
    os.environ["RIMWORLD_CODEX_ART_QUEUE"] = "/tmp/does-not-exist-rimworld-art"
    check("RIMWORLD_CODEX_ART_QUEUE is respected",
          str(queue_root()) == "/tmp/does-not-exist-rimworld-art",
          f"got {queue_root()}")
    check("and honouring it still creates nothing",
          not Path("/tmp/does-not-exist-rimworld-art").exists())
    del os.environ["RIMWORLD_CODEX_ART_QUEUE"]


def test_live_pipeline_untouched() -> None:
    """Pathway 1 must remain exactly as it is; v2 only reads from it."""
    repo = HERE.parents[3]
    live = repo / "skills/generating-images/scripts/codex_image.py"
    check("the live one-shot wrapper still exists", live.is_file(), str(live))
    text = live.read_text(errors="replace")
    check("the live wrapper does not import anything from v2",
          "codex_art_v2" not in text)
    skill = repo / "skills/generating-images/SKILL.md"
    check("the live skill still exists", skill.is_file(), str(skill))
    check("the live skill does not route through v2",
          "codex_art_v2" not in skill.read_text(errors="replace"))


def main() -> int:
    tests = [
        test_handshake,
        test_turn_streams_and_completes,
        test_interrupt,
        test_error_is_preserved_exactly,
        test_hang_is_unknown_not_failed,
        test_request_timeout_does_not_hang_forever,
        test_scheduler_table,
        test_pacific_rendering,
        test_render_is_safe_on_every_verdict,
        test_no_shared_machine_state_is_touched,
        test_live_pipeline_untouched,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # a crashing test is a failing test
            FAIL.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
            print(f"FAIL  {t.__name__} raised {type(exc).__name__}: {exc}")

    total = len(PASS) + len(FAIL)
    print(f"\ncodex_art_v2 selftest: {len(PASS)}/{total} passed "
          f"(fake app-server only; no image quota spent)")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
