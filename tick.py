#!/usr/bin/env python3
"""Run one paper-trading tick: scan, filter, risk, snipe, fill, exits.

    python tick.py

Paper money only. Never places a real order and never needs API keys.

Takes an exclusive file lock (tick.lock) so this process never overlaps
another tick — including the 5-minute engine loop and any scheduled
assistant routine.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCK_PATH = ROOT / "tick.lock"

from engine import run_tick  # noqa: E402


def acquire_tick_lock():
    """Return an open file holding an exclusive lock, or None if busy."""
    LOCK_PATH.touch(exist_ok=True)
    fp = open(LOCK_PATH, "r+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return None
    fp.seek(0)
    fp.truncate()
    fp.write(f"{os.getpid()}\n")
    fp.flush()
    try:
        os.fsync(fp.fileno())
    except OSError:
        pass
    return fp


def main() -> int:
    lock = acquire_tick_lock()
    if lock is None:
        print("Another tick is already running; skipping.", flush=True)
        return 0
    try:
        state = run_tick()
        # Non-zero only on a hard failure (run_tick raises). A quiet tick with no
        # fills is still a successful run.
        return 0 if state else 1
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
