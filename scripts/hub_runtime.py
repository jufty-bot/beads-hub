#!/usr/bin/env python3
"""Wait for Dolt or the initialized hub before starting a dependent service."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import time
from pathlib import Path


def configure_remote_access() -> None:
    """Provision the database-scoped remote account without putting its password in argv."""
    password = os.environ.get("BEADS_REMOTE_PASSWORD", "")
    if not password or "\x00" in password:
        raise ValueError("Inject a nonempty BEADS_REMOTE_PASSWORD for the remote SQL account")
    escaped = password.replace("\\", "\\\\").replace("'", "''")
    sql = (
        "CREATE USER IF NOT EXISTS 'beads'@'%';\n"
        f"ALTER USER 'beads'@'%' IDENTIFIED BY '{escaped}';\n"
        "GRANT ALL PRIVILEGES ON beads_hub.* TO 'beads'@'%';\n"
    )
    result = subprocess.run(
        [
            "dolt",
            "--host",
            os.environ.get("BEADS_DOLT_SERVER_HOST", "127.0.0.1"),
            "--port",
            os.environ.get("BEADS_DOLT_SERVER_PORT", "3307"),
            "--user",
            "root",
            "--no-tls",
            "sql",
        ],
        input=sql,
        env={**os.environ, "DOLT_CLI_PASSWORD": ""},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not provision remote SQL account; SQL output suppressed")


def wait_for_hub(*, sql_only: bool = False, timeout: float = 120) -> None:
    """Require a live database, failing startup if initialization never completes."""
    deadline = time.monotonic() + timeout
    workspace = Path(os.environ.get("BEADS_WORKING_DIR", "/var/lib/beads"))
    while time.monotonic() < deadline:
        if sql_only:
            try:
                with socket.create_connection(
                    (
                        os.environ.get("BEADS_DOLT_SERVER_HOST", "127.0.0.1"),
                        int(os.environ.get("BEADS_DOLT_SERVER_PORT", "3307")),
                    ),
                    timeout=2,
                ):
                    return
            except OSError:
                pass
        elif (workspace / ".beads/metadata.json").is_file():
            result = subprocess.run(
                [os.environ.get("BEADS_PATH", "bd"), "info", "--json"],
                cwd=workspace,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return
        time.sleep(1)
    raise TimeoutError("Dolt or hub initialization did not become ready within the startup timeout")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", action="store_true", help="Wait only for the SQL listener")
    parser.add_argument(
        "--configure-remote", action="store_true", help="Provision remote SQL access"
    )
    args = parser.parse_args()
    if args.configure_remote:
        configure_remote_access()
    else:
        wait_for_hub(sql_only=args.sql)
