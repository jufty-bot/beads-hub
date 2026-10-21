"""Persist polling progress independently of source snapshots and local Beads triage."""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OVERLAP_SECONDS = 300
"""Replay five minutes of source updates to tolerate timestamp ties and moving pages."""

FULL_SCAN_SECONDS = 3600
"""Repair backlog coverage with a full open-item inventory at least hourly."""


class SyncState:
    """Keep per-repository checkpoints and a shared rate-limit deadline under the sync lock."""

    def __init__(self, path: Path | None = None) -> None:
        """Load existing progress, or start a full bootstrap without creating a database."""
        self.path = path
        self._saved: str | None = None
        self.data: dict[str, Any] = {"version": 1, "repositories": {}, "retry_at": 0}
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("repositories"), dict)
            ):
                raise ValueError("Invalid GitHub sync state; restore or remove the checkpoint file")
            for entry in data["repositories"].values():
                if not isinstance(entry, dict):
                    raise ValueError("Invalid repository checkpoint")
                for field in ("checkpoint", "last_full_at", "last_success_at"):
                    self.timestamp(entry.get(field, 0))
            self.timestamp(data.get("retry_at", 0))
            self.data = data
            self._saved = json.dumps(data, sort_keys=True) + "\n"

    @staticmethod
    def timestamp(value: Any) -> float:
        """Reject invalid progress rather than skipping an unknown range of source changes."""
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Invalid GitHub sync timestamp")
        return float(value)

    def since(self, repo: dict[str, Any], *, now: float) -> str | None:
        """Choose bootstrap/repair or a delta starting before the last committed pass."""
        entry = self.data["repositories"].get(repo["node_id"], {})
        checkpoint = self.timestamp(entry.get("checkpoint", 0))
        last_full = self.timestamp(entry.get("last_full_at", 0))
        if (
            not checkpoint
            or not last_full
            or checkpoint > now
            or last_full > now
            or now - last_full >= FULL_SCAN_SECONDS
            or entry.get("name") != repo["full_name"]
        ):
            return None
        return datetime.fromtimestamp(max(0, checkpoint - OVERLAP_SECONDS), UTC).isoformat()

    def mark_success(
        self, repo: dict[str, Any], *, started: float, finished: float, full: bool
    ) -> None:
        """Stage successful coverage for the pass's final atomic save."""
        entry = self.data["repositories"].setdefault(repo["node_id"], {})
        entry.update(
            name=repo["full_name"], checkpoint=started, last_success_at=finished, error=None
        )
        if full:
            entry["last_full_at"] = started

    def mark_failure(self, repo: dict[str, Any], message: str) -> None:
        """Stage an error without advancing the repository's last good checkpoint."""
        entry = self.data["repositories"].setdefault(repo["node_id"], {})
        entry.setdefault("name", repo["full_name"])
        entry["error"] = message

    def save(self) -> None:
        """Atomically commit changed progress, leaving a complete old or new document on disk."""
        if self.path is None:
            return
        content = json.dumps(self.data, sort_keys=True) + "\n"
        if content == self._saved:
            return
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._saved = content
