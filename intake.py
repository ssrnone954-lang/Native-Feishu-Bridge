"""Message intake — queue, debounce, and preemption management."""

import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional, Callable

import voice_pipeline


class ActiveRun:
    """Represents a running Claude process for a chat."""
    def __init__(self, chat_id: str, proc):
        self.chat_id = chat_id
        self.proc = proc
        self.started_at = time.time()
        self.interrupted = False

    def interrupt(self):
        """Interrupt this run (new message received)."""
        self.interrupted = True
        from claude_runner import kill_proc
        kill_proc(self.proc)


class ActiveRuns:
    """Track active Claude runs per chat scope."""

    def __init__(self):
        self._runs: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()

    def register(self, scope: str, proc) -> ActiveRun:
        run = ActiveRun(scope, proc)
        with self._lock:
            # Interrupt any existing run for this scope
            old = self._runs.get(scope)
            if old:
                old.interrupt()
            self._runs[scope] = run
        return run

    def unregister(self, scope: str, run: ActiveRun):
        with self._lock:
            if self._runs.get(scope) is run:
                del self._runs[scope]

    def interrupt(self, scope: str) -> bool:
        """Interrupt the active run for a scope. Returns True if one was interrupted."""
        with self._lock:
            run = self._runs.get(scope)
            if run:
                run.interrupt()
                return True
        return False

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._runs)


class PendingQueue:
    """Debounced message queue.

    Rapid messages within `debounce_ms` are batched.
    When the debounce timer fires, the batch is flushed via the callback.
    """

    def __init__(self, debounce_ms: int = 800,
                 callback: Callable[[str, list[dict]], None] = None):
        self.debounce_ms = debounce_ms / 1000.0
        self.callback = callback
        self._timers: dict[str, threading.Timer] = {}
        self._buffers: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def enqueue(self, scope: str, msg_data: dict):
        """Add a message to the queue for a scope."""
        with self._lock:
            if scope not in self._buffers:
                self._buffers[scope] = []
            self._buffers[scope].append(msg_data)

            # Cancel existing timer, start new one
            old = self._timers.pop(scope, None)
            if old:
                old.cancel()

            timer = threading.Timer(self.debounce_ms, self._flush, args=[scope])
            self._timers[scope] = timer
            timer.daemon = True
            timer.start()

    def _flush(self, scope: str):
        with self._lock:
            batch = self._buffers.pop(scope, [])
            self._timers.pop(scope, None)

        if batch and self.callback:
            self.callback(scope, batch)

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buffers.values())


def process_audio_message(audio_path: str, cfg: dict) -> Optional[str]:
    """Downloaded audio → transcribe + summarize → cleaned text."""
    if not cfg.get("voice", {}).get("enabled"):
        return None
    if not voice_pipeline.is_ready():
        print("[intake] Voice pipeline not initialized, skipping audio", file=sys.stderr)
        return None
    return voice_pipeline.process(audio_path)
