#!/usr/bin/env python3
"""fake_appserver.py — a stand-in for `codex app-server`, for tests.

Per CODEX_PROPOSAL_GRAPHICS_WORKFLOW.md's own checklist item 9: "Tests using a
fake app-server/event stream; no real image generation in ordinary tests." This
is that fake. It speaks the same newline-delimited JSON-RPC and NEVER calls
OpenAI, so the whole client can be exercised for free, repeatably, and offline.

It deliberately reproduces the REAL server's quirks, measured from a live
0.153.1 handshake on 2026-09-06 -- a fake that is politer than the real thing
tests nothing:

  * responses and notifications omit `"jsonrpc"`;
  * an unsolicited notification arrives right after `initialize`;
  * `turn/start` returns immediately and completion comes later as a
    `turn/completed` notification, with `item/*` events streamed in between.

Scenarios (env var FAKE_APPSERVER_SCENARIO):
  ok            initialize, healthy limits, a turn that streams and completes
  busy          limits at 92% weekly -- the scheduler must refuse to dispatch
  unknown       limits with nulls -- must be treated as unknown, not zero
  error         turn/start returns a JSON-RPC error with an exact payload
  hang          turn never completes -- exercises timeout_outcome_unknown
  interrupt     turn completes as interrupted after turn/interrupt is received
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

SCENARIO = os.environ.get("FAKE_APPSERVER_SCENARIO", "ok")

LIMITS = {
    "ok": {"primary": 23, "secondary": 62},
    "busy": {"primary": 40, "secondary": 92},
    "warn": {"primary": 30, "secondary": 82},
    "unknown": {"primary": None, "secondary": None},
    "error": {"primary": 10, "secondary": 10},
    "hang": {"primary": 10, "secondary": 10},
    "interrupt": {"primary": 10, "secondary": 10},
}

_out_lock = threading.Lock()


def emit(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def limits_result() -> dict:
    cfg = LIMITS.get(SCENARIO, LIMITS["ok"])
    now = int(time.time())

    def window(used, mins, offset):
        if used is None:
            return {"usedPercent": None, "windowDurationMins": None, "resetsAt": None}
        return {"usedPercent": used, "windowDurationMins": mins,
                "resetsAt": now + offset}

    block = {
        "limitId": "codex",
        "limitName": None,
        "primary": window(cfg["primary"], 300, 3600),
        "secondary": window(cfg["secondary"], 10080, 86400),
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "plus",
        "rateLimitReachedType": None,
    }
    return {
        "rateLimits": block,
        "rateLimitsByLimitId": {"codex": block},
        "rateLimitResetCredits": {"availableCount": 3, "credits": []},
    }


def stream_turn(thread_id: str, turn_id: str) -> None:
    """Emit the notification sequence a real turn produces."""
    if SCENARIO == "hang":
        return  # never completes, on purpose
    time.sleep(0.15)
    emit({"method": "turn/started",
          "params": {"threadId": thread_id, "turnId": turn_id}})
    time.sleep(0.1)
    emit({"method": "item/started",
          "params": {"threadId": thread_id, "item": {"type": "toolCall",
                                                     "name": "image_gen"}}})
    time.sleep(0.1)
    emit({"method": "item/completed",
          "params": {"threadId": thread_id,
                     "item": {"type": "toolCall", "name": "image_gen",
                              "output": {"path": "C:\\fake\\generated.png"}}}})
    if SCENARIO == "interrupt":
        return  # completion only after turn/interrupt arrives
    time.sleep(0.1)
    emit({"method": "turn/completed",
          "params": {"threadId": thread_id, "turnId": turn_id,
                     "status": "completed"}})


def main() -> int:
    threads: dict[str, int] = {}
    turn_seq = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")

        if method == "initialize":
            emit({"id": rid, "result": {
                "userAgent": "fake/0.0.0", "codexHome": "C:\\fake\\.codex",
                "platformFamily": "windows", "platformOs": "windows"}})
            # The real server volunteers this before anything asks.
            emit({"method": "remoteControl/status/changed",
                  "params": {"status": "disabled"}, "emittedAtMs": 0})
        elif method == "initialized":
            pass  # notification, no reply
        elif method == "account/rateLimits/read":
            emit({"id": rid, "result": limits_result()})
        elif method == "thread/start":
            tid = f"thread-{len(threads) + 1}"
            threads[tid] = 0
            emit({"id": rid, "result": {
                "thread": {"id": tid}, "cwd": "C:\\fake", "model": "fake",
                "modelProvider": "fake", "approvalPolicy": "never",
                "approvalsReviewer": None, "sandbox": "workspace-write"}})
        elif method == "turn/start":
            if SCENARIO == "error":
                emit({"id": rid, "error": {
                    "code": -32000,
                    "message": "UsageLimitExceeded: weekly limit reached",
                    "data": {"httpStatus": 429}}})
                continue
            turn_seq += 1
            turn_id = f"turn-{turn_seq}"
            thread_id = (msg.get("params") or {}).get("threadId", "thread-1")
            emit({"id": rid, "result": {"turn": {"id": turn_id}}})
            threading.Thread(target=stream_turn, args=(thread_id, turn_id),
                             daemon=True).start()
        elif method == "turn/steer":
            emit({"id": rid, "result": {"accepted": True}})
        elif method == "turn/interrupt":
            params = msg.get("params") or {}
            emit({"id": rid, "result": {"interrupted": True}})
            emit({"method": "turn/completed",
                  "params": {"threadId": params.get("threadId"),
                             "turnId": params.get("turnId"),
                             "status": "interrupted"}})
        else:
            emit({"id": rid, "error": {"code": -32601,
                                       "message": f"method not found: {method}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
