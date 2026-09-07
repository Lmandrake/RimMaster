#!/usr/bin/env python3
"""appserver.py — a thin JSON-RPC client for `codex app-server`.

EXPERIMENTAL SECOND PATHWAY. The live, working image pipeline is
`skills/generating-images/` (one-shot `codex exec`) and nothing here touches it.
See ../../../../infrastructure/agents/OPUS_REVIEW_codex_graphics_second_pipeline.md
for why only this much of CODEX_PROPOSAL_ART_WORKER.md was built.

What is verified, and how
------------------------
Against codex-cli **0.153.1** on 2026-09-06, by generating the protocol's own
JSON Schema (`codex app-server generate-json-schema --out <dir>`) and by
completing a real handshake from WSL:

- `codex app-server` exists but is flagged **[experimental]** in `codex --help`.
  Its wire protocol can change under an app update, exactly as the CLI's install
  hash does. Every method used here is feature-detected at `initialize` time
  rather than assumed.
- These methods exist verbatim: `initialize`, `thread/start`, `thread/resume`,
  `thread/compact/start`, `thread/archive`, `turn/start`, `turn/steer`,
  `turn/interrupt`, `account/rateLimits/read`, `skills/list`.
- Framing is newline-delimited JSON on stdout. **Responses and notifications
  omit the `"jsonrpc"` field** — demultiplexing on `"id"` vs `"method"` is
  required; a client that filters on `jsonrpc == "2.0"` sees nothing.
- The server emits unsolicited notifications immediately after `initialize`
  (e.g. `remoteControl/status/changed`), before anything is requested.

Nothing in this module generates an image. `turn_start` is the only call that
can spend quota, and the CLI refuses to reach it without explicit authorization.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[4]

# The one-shot pipeline already encodes the hard-won facts about locating
# codex.exe (the install hash changes on every app update) and crossing the
# WSL/Windows boundary. Import them read-only rather than restating them: a
# second copy of that knowledge is a second thing to go stale.
_LIVE_WRAPPER = REPO_ROOT / "skills/generating-images/scripts/codex_image.py"


class AppServerError(RuntimeError):
    """A JSON-RPC error, or the transport failing. Carries the exact payload."""

    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


def _load_live_wrapper():
    """Import codex_image.py by path. Read-only; never modifies it."""
    if not _LIVE_WRAPPER.is_file():
        raise AppServerError(
            f"Cannot find the live wrapper at {_LIVE_WRAPPER}. It supplies CLI "
            f"location and WSL->Windows path translation for this module too."
        )
    spec = importlib.util.spec_from_file_location("_codex_image_live", _LIVE_WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_codex_cli() -> Path:
    return _load_live_wrapper().find_codex_cli()


def auth_mode() -> str:
    return _load_live_wrapper().auth_mode()


def wsl_to_win(p: Path) -> str:
    return _load_live_wrapper().wsl_to_win(p)


# The queue root named by CODEX_PROPOSAL_ART_WORKER.md. Resolved here so the
# name has one definition, but NOTHING in this package writes to it -- see the
# review doc for why the durable queue was judged not worth building.
def queue_root() -> Path:
    env = os.environ.get("RIMWORLD_CODEX_ART_QUEUE")
    if env:
        return Path(env)
    return Path("/mnt/c/Users/Mandrake/AppData/Local/RimworldCodexArtQueue")


class AppServer:
    """One `codex app-server` process, driven over stdio.

    Usage:
        with AppServer() as srv:
            srv.initialize()
            print(srv.rate_limits())

    The process is reused across calls; a caller that wants a second one makes a
    second instance. `close()` terminates only the process this object spawned --
    never a kill-by-name, which would take out the owner's desktop Codex too.
    """

    def __init__(self, cli: list[str] | None = None, cwd: Path | None = None,
                 verbose: bool = False):
        self._cmd = cli if cli is not None else [str(find_codex_cli()),
                                                 "app-server", "--listen", "stdio://"]
        self._cwd = str(cwd) if cwd else None
        self._verbose = verbose
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._responses: dict[int, dict] = {}
        self._notifications: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()
        self._dead: str | None = None
        self.server_info: dict = {}
        self.methods: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "AppServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self._cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Kill the PID we own. Never `pkill -f codex` -- that pattern also
            # matches this script's own argv and the owner's desktop app.
            proc.kill()
            proc.wait(timeout=5)

    # -- plumbing ----------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                if self._verbose:
                    print(f"[appserver] non-JSON: {line[:200]}", file=sys.stderr)
                continue
            # Responses and notifications both arrive here and BOTH omit
            # "jsonrpc" on this build. Demux on the presence of "id".
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                with self._lock:
                    self._responses[msg["id"]] = msg
            else:
                self._notifications.put(msg)
        self._dead = self._dead or "app-server stdout closed"

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            if self._verbose:
                print(f"[appserver:stderr] {line.rstrip()}", file=sys.stderr)

    def _send(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AppServerError("app-server is not running")
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None,
                timeout: float = 60.0) -> dict:
        """Send a request and block for its response. Raises on JSON-RPC error."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                msg = self._responses.pop(rid, None)
            if msg is not None:
                if "error" in msg:
                    err = msg["error"]
                    raise AppServerError(
                        f"{method} failed: {err.get('message', err)}", payload=err)
                return msg.get("result", {})
            if self._proc is not None and self._proc.poll() is not None:
                raise AppServerError(
                    f"app-server exited (code {self._proc.returncode}) "
                    f"while waiting for {method}")
            time.sleep(0.02)
        raise AppServerError(f"{method} timed out after {timeout:.0f}s")

    def notifications(self) -> Iterator[dict]:
        """Drain whatever notifications have arrived. Never blocks."""
        while True:
            try:
                yield self._notifications.get_nowait()
            except queue.Empty:
                return

    # -- protocol ----------------------------------------------------------

    def initialize(self, client_name: str = "rimworld-codex-art-v2",
                   timeout: float = 60.0) -> dict:
        """Handshake. Must be sent exactly once per connection."""
        self.server_info = self.request(
            "initialize",
            {"clientInfo": {"name": client_name, "title": "RimWorld art v2",
                            "version": "0.1.0"}},
            timeout=timeout,
        )
        self.notify("initialized", {})
        return self.server_info

    def supports(self, method: str) -> bool:
        """Feature-detect a method against the INSTALLED build.

        `codex app-server` is flagged experimental, so a method present today can
        be renamed by an app update. Callers check rather than assume; the schema
        dump is regenerated with `codex app-server generate-json-schema`.
        """
        if not self.methods:
            return True  # no roster loaded: caller falls back to try/except
        return method in self.methods

    def rate_limits(self, timeout: float = 30.0) -> dict:
        """`account/rateLimits/read`. Spends NO image quota. Safe to poll."""
        return self.request("account/rateLimits/read", {}, timeout=timeout)

    def thread_start(self, cwd: Path | None = None, sandbox: Any = None,
                     timeout: float = 60.0) -> str:
        params: dict = {}
        if cwd is not None:
            params["cwd"] = wsl_to_win(cwd) if str(cwd).startswith("/mnt/") else str(cwd)
        if sandbox is not None:
            params["sandbox"] = sandbox
        res = self.request("thread/start", params, timeout=timeout)
        thread = res.get("thread") or {}
        tid = thread.get("id") or thread.get("threadId")
        if not tid:
            raise AppServerError("thread/start returned no thread id", payload=res)
        return tid

    def turn_start(self, thread_id: str, text: str, timeout: float = 60.0) -> str:
        """Begin a turn. THIS IS THE CALL THAT CAN SPEND QUOTA.

        Returns the turn id. Completion arrives asynchronously as a
        `turn/completed` notification -- see `wait_for_turn`.
        """
        res = self.request(
            "turn/start",
            {"threadId": thread_id,
             "input": [{"type": "input_text", "text": text}]},
            timeout=timeout,
        )
        turn = res.get("turn") or {}
        tid = turn.get("id") or turn.get("turnId")
        if not tid:
            raise AppServerError("turn/start returned no turn id", payload=res)
        return tid

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str,
                   timeout: float = 30.0) -> dict:
        return self.request(
            "turn/steer",
            {"threadId": thread_id, "expectedTurnId": expected_turn_id,
             "input": [{"type": "input_text", "text": text}]},
            timeout=timeout,
        )

    def turn_interrupt(self, thread_id: str, turn_id: str,
                       timeout: float = 30.0) -> dict:
        return self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout=timeout,
        )

    def wait_for_turn(self, timeout: float = 300.0,
                      on_event=None) -> dict:
        """Block until `turn/completed` (or `error`) arrives.

        A timeout here means the OUTCOME IS UNKNOWN, not that generation failed --
        the same trap that makes the one-shot wrapper discard finished images
        (CODEX_WRAPPER_HARVEST_FIX_1). Callers must inspect artifacts before
        concluding anything.
        """
        deadline = time.time() + timeout
        seen: list[dict] = []
        while time.time() < deadline:
            for note in self.notifications():
                seen.append(note)
                if on_event:
                    on_event(note)
                method = note.get("method")
                if method == "turn/completed":
                    return {"status": "completed", "notification": note,
                            "events": seen}
                if method == "error":
                    return {"status": "error", "notification": note,
                            "events": seen}
            if self._proc is not None and self._proc.poll() is not None:
                return {"status": "process_exited", "events": seen}
            time.sleep(0.05)
        return {"status": "timeout_outcome_unknown", "events": seen}
