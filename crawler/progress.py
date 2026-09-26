from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProgressSnapshot:
    pages_saved_run: int
    pages_saved_total: int
    text_bytes_total: int
    target_bytes: int
    pages_per_sec: float
    mb_per_sec: float
    eta_seconds: float | None
    active_domains: int
    in_flight: int
    errors: int
    blocked_domains: int


class ProgressReporter:
    """Daemon thread that prints a single progress line every N seconds."""

    def __init__(
        self,
        *,
        interval: float,
        target_bytes: int,
        get_snapshot: Callable[[], ProgressSnapshot],
        stop_event: threading.Event,
    ) -> None:
        self._interval = max(1.0, interval)
        self._target_bytes = target_bytes
        self._get_snapshot = get_snapshot
        self._stop_event = stop_event
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="progress-reporter",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                snap = self._get_snapshot()
            except Exception as exc:
                print(f"PROGRESS error={exc}", flush=True)
                continue
            self._print_line(snap)

    def _print_line(self, snap: ProgressSnapshot) -> None:
        gb = snap.text_bytes_total / (1024 ** 3)
        target_gb = self._target_bytes / (1024 ** 3) if self._target_bytes else 0.0
        if self._target_bytes > 0:
            pct = 100.0 * snap.text_bytes_total / self._target_bytes
            if snap.eta_seconds is not None and snap.eta_seconds >= 0:
                eta = _fmt_duration(snap.eta_seconds)
            else:
                eta = "?"
            target_part = f"{gb:.2f}/{target_gb:.1f}GB ({pct:.1f}%) eta={eta}"
        else:
            target_part = f"{gb:.2f}GB (no target)"

        now = time.strftime("%H:%M:%S")
        print(
            f"PROGRESS {now} saved={snap.pages_saved_run}/{snap.pages_saved_total} "
            f"text={target_part} "
            f"rate={snap.pages_per_sec:.2f}p/s {snap.mb_per_sec:.2f}MB/s "
            f"domains={snap.active_domains} inflight={snap.in_flight} "
            f"errors={snap.errors} blocked={snap.blocked_domains}",
            flush=True,
        )


def print_startup_banner(
    *,
    seeds_count: int,
    hosts_count: int,
    threads: int,
    target_gb: float,
    db_path: str,
    text_root: str,
    resuming: bool,
    free_disk_gb: float,
) -> None:
    mode = "resuming" if resuming else "fresh"
    print(
        f"CRAWLER seeds={seeds_count} hosts={hosts_count} threads={threads} "
        f"target={target_gb:g}GB db={db_path} text_root={text_root} "
        f"mode={mode} free_disk={free_disk_gb:.1f}GB",
        flush=True,
    )


def free_disk_bytes(path: str) -> int:
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    usage = shutil.disk_usage(str(target if target.exists() else Path.cwd()))
    return int(usage.free)


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
