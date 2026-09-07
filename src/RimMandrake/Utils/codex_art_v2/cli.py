#!/usr/bin/env python3
"""cli.py — the experimental second graphics pathway.

    python3 src/RimMandrake/Utils/codex_art_v2/cli.py probe     # no quota
    python3 src/RimMandrake/Utils/codex_art_v2/cli.py usage     # no quota
    python3 src/RimMandrake/Utils/codex_art_v2/cli.py generate  # REFUSED without authorization

PATHWAY 1 (live, working, unchanged) is `skills/generating-images/` -- one-shot
`codex exec`. Nothing here replaces it or is imported by it. See
`infrastructure/agents/OPUS_REVIEW_codex_graphics_second_pipeline.md`.

`probe` and `usage` are safe to run at any time: they complete a handshake and
read account state, and spend no image quota. `usage` is useful TODAY, to the
EXISTING pipeline, without adopting anything else here -- run it before
launching a batch driver.

`generate` is a plumbing test, not a production route. It refuses to run without
`--owner-authorized "<the owner's verbatim words>"`, because a real turn spends
the owner's Codex quota and only the owner can authorize that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scheduler  # noqa: E402
from appserver import AppServer, AppServerError, auth_mode, find_codex_cli, queue_root  # noqa: E402


def cmd_probe(args) -> int:
    """Is the app-server transport reachable? Spends no quota."""
    try:
        cli = find_codex_cli()
    except AppServerError as exc:
        print(f"FAIL codex cli      {exc}")
        return 2
    print(f"OK   codex cli      {cli}")
    print(f"OK   auth mode      {auth_mode()}")
    print(f"     queue root     {queue_root()}  (resolved only; nothing writes here)")

    try:
        with AppServer(verbose=args.verbose) as srv:
            info = srv.initialize(timeout=args.timeout)
            print(f"OK   initialize     {info.get('userAgent', '?')}")
            print(f"OK   codexHome      {info.get('codexHome', '?')}")
            notes = [n.get("method") for n in srv.notifications() if n.get("method")]
            if notes:
                print(f"     unsolicited    {', '.join(notes)}")
    except AppServerError as exc:
        print(f"FAIL app-server     {exc}")
        print("     `codex app-server` is flagged EXPERIMENTAL; an app update can")
        print("     rename or remove it. Regenerate the protocol schema with")
        print("     `codex app-server generate-json-schema --out <dir>` and compare.")
        return 2
    return 0


def cmd_usage(args) -> int:
    """Read live account limits and print the batch verdict. Spends no quota."""
    try:
        with AppServer(verbose=args.verbose) as srv:
            srv.initialize(timeout=args.timeout)
            raw = srv.rate_limits(timeout=args.timeout)
    except AppServerError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(raw, indent=2))
        return 0

    verdict = scheduler.decide(raw)
    print(scheduler.render(verdict))
    return 0 if verdict.may_dispatch else 1


def cmd_generate(args) -> int:
    """A single real turn. GATED: only the owner can authorize spending quota."""
    if not args.owner_authorized:
        print(
            "REFUSED. A real turn spends the owner's Codex image quota, and no\n"
            "agent can authorize that on his behalf.\n\n"
            "This pathway has NOT had its one real smoke test. Everything else\n"
            "here (probe, usage, the selftest against the fake app-server) has\n"
            "passed without spending anything.\n\n"
            "If the owner says to run it, pass his verbatim words:\n"
            '  --owner-authorized "<what he said>"\n\n'
            "Before that first real run, prefer PATHWAY 1 -- it works:\n"
            "  python3 skills/generating-images/scripts/codex_image.py generate ...",
            file=sys.stderr,
        )
        return 3

    print(f"authorization on record: {args.owner_authorized!r}")
    try:
        with AppServer(verbose=args.verbose) as srv:
            srv.initialize(timeout=args.timeout)
            verdict = scheduler.decide(srv.rate_limits(timeout=args.timeout))
            print(scheduler.render(verdict))
            if not verdict.may_dispatch:
                print("\nSTOPPING before the turn: the scheduler refuses to dispatch.",
                      file=sys.stderr)
                return 1
            thread_id = srv.thread_start(cwd=Path(args.cwd) if args.cwd else None,
                                         timeout=args.timeout)
            print(f"thread: {thread_id}")
            turn_id = srv.turn_start(thread_id, args.prompt, timeout=args.timeout)
            print(f"turn:   {turn_id}")
            result = srv.wait_for_turn(
                timeout=args.turn_timeout,
                on_event=lambda n: print(f"  event {n.get('method')}"))
            print(f"status: {result['status']}")
            if result["status"] == "timeout_outcome_unknown":
                print(
                    "\nThe OUTCOME IS UNKNOWN, not failed. An image may exist under\n"
                    "$CODEX_HOME/generated_images. Inspect before retrying -- this is\n"
                    "exactly the trap CODEX_WRAPPER_HARVEST_FIX_1 records.",
                    file=sys.stderr)
                return 1
    except AppServerError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        if exc.payload is not None:
            print(f"exact payload: {json.dumps(exc.payload)}", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-request timeout in seconds")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="is the app-server reachable? (no quota)")
    p.set_defaults(func=cmd_probe)

    u = sub.add_parser("usage", help="live limits + batch verdict (no quota)")
    u.add_argument("--json", action="store_true", help="raw limits, unjudged")
    u.set_defaults(func=cmd_usage)

    g = sub.add_parser("generate", help="one real turn (owner authorization required)")
    g.add_argument("--prompt", default="Reply with the single word: ready.")
    g.add_argument("--cwd", default=None)
    g.add_argument("--turn-timeout", type=float, default=300.0)
    g.add_argument("--owner-authorized", default=None, metavar="WORDS",
                   help="the owner's verbatim authorization to spend quota")
    g.set_defaults(func=cmd_generate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
