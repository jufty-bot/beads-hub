"""Verify remote SQL provisioning keeps injected credentials out of process arguments."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from hub_runtime import configure_remote_access


def test_remote_password_passed_only_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escape SQL literals and suppress credential-bearing client output."""
    password = "synthetic'\\password"
    monkeypatch.setenv("BEADS_REMOTE_PASSWORD", password)

    def sql_client(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Inspect process boundaries without opening a database connection."""
        assert all(password not in arg for arg in args)
        assert "synthetic''\\\\password" in kwargs["input"]
        assert "GRANT ALL PRIVILEGES ON beads_hub.*" in kwargs["input"]
        assert kwargs["env"]["DOLT_CLI_PASSWORD"] == ""
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr(subprocess, "run", sql_client)
    configure_remote_access()


def test_missing_remote_password_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not provision a remotely accessible account with a blank password."""
    monkeypatch.delenv("BEADS_REMOTE_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="nonempty"):
        configure_remote_access()


def test_sql_errors_do_not_echo_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SQL parse failure must not leak its source query in the startup error."""
    monkeypatch.setenv("BEADS_REMOTE_PASSWORD", "synthetic-password")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, returncode=1, stderr="query containing synthetic-password"
        ),
    )
    with pytest.raises(RuntimeError, match="output suppressed") as error:
        configure_remote_access()
    assert "synthetic-password" not in str(error.value)
