"""Verify installed commands work outside the source checkout."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_sync_status_outside_checkout(tmp_path: Path) -> None:
    """Read polling progress through the installed command without GitHub or source-path setup."""
    result = subprocess.run(
        ["beads-hub-sync", "--status"],
        cwd=tmp_path,
        env={**os.environ, "BEADS_WORKING_DIR": str(tmp_path), "GH_TOKEN": "", "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == {"version": 1, "repositories": {}, "retry_at": 0}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["beads-hub-sync", "beads-hub-runtime"])
def test_command_help_outside_checkout(tmp_path: Path, command: str) -> None:
    """Resolve console entrypoints without starting services or requiring a hub workspace."""
    result = subprocess.run(
        [command, "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "usage:" in result.stdout and command in result.stdout
