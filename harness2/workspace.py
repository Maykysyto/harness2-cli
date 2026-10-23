# Copyright 2026 The Harness2 Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Temporary project worktrees and file workspaces for rollouts and judges."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .settings import ConfigError

# Rollouts run in threads. `git worktree prune` racing another thread's `worktree add` can
# delete a worktree that is still being created, so worktree bookkeeping is serialized.
_WORKTREES = threading.Lock()


@contextmanager
def project_workspace(project: Path, ref: str) -> Iterator[Path]:
    ws = Path(tempfile.mkdtemp(prefix="harness2-ws-")).resolve()
    try:
        with _WORKTREES:
            _add_worktree(project, ws, ref)
        yield ws
    finally:
        with _WORKTREES:
            subprocess.run(
                ["git", "-C", str(project), "worktree", "remove", "--force", str(ws)],
                capture_output=True,
            )
        shutil.rmtree(ws, ignore_errors=True)


def _add_worktree(project: Path, ws: Path, ref: str) -> None:
    pruned = subprocess.run(
        ["git", "-C", str(project), "worktree", "prune"], capture_output=True, text=True
    )
    if pruned.returncode:
        raise ConfigError(f"cannot prune project worktrees: {pruned.stderr.strip()}")
    result = subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", "--", str(ws), ref],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ConfigError(f"cannot create project workspace at {ref!r}: {result.stderr.strip()}")


def persistent_project_workspace(project: Path, ref: str, ws: Path) -> Path:
    """Create a rollout worktree once; the caller keeps it until the run is closed."""
    ws = Path(ws).resolve()
    if ws.exists():
        if not (ws / ".git").exists():
            raise ConfigError(f"incomplete project workspace at {ws}; inspect before recovery")
        return ws
    ws.parent.mkdir(parents=True, exist_ok=True)
    with _WORKTREES:
        _add_worktree(project, ws, ref)
    return ws


def discard_unprepared_project_workspace(project: Path, ws: Path) -> None:
    """Remove a worktree whose Pi preparation never reached its durable marker."""
    ws = Path(ws).resolve()
    with _WORKTREES:
        subprocess.run(
            ["git", "-C", str(project), "worktree", "remove", "--force", str(ws)],
            capture_output=True,
        )
    shutil.rmtree(ws, ignore_errors=True)


def persistent_files_workspace(src: Path | None, ws: Path) -> Path:
    """Copy task inputs only on the first attempt of a durable rollout."""
    ws = Path(ws).resolve()
    if ws.exists():
        return ws
    ws.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f"{ws.name}.staging-", dir=ws.parent))
    try:
        if src is not None:
            shutil.copytree(src, staged, dirs_exist_ok=True)
        (staged / "output").mkdir(exist_ok=True)
        os.replace(staged, ws)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return ws


@contextmanager
def files_workspace(src: Path | None) -> Iterator[Path]:
    ws = Path(tempfile.mkdtemp(prefix="harness2-ws-")).resolve()
    try:
        if src is not None:
            shutil.copytree(src, ws, dirs_exist_ok=True)
        (ws / "output").mkdir(exist_ok=True)
        yield ws
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def commit_all(ws: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=harness2",
            "-c",
            "user.email=harness2@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "--allow-empty",
            "-qm",
            message,
        ],
        cwd=ws,
        check=True,
        capture_output=True,
    )


def diff_since(ws: Path, base: str, exclude: Sequence[str]) -> str:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
    pathspecs = [
        p.replace(":(", ":(exclude,", 1) if p.startswith(":(") else f":(exclude){p}"
        for p in exclude
    ]
    return subprocess.run(
        ["git", "diff", "--cached", "--binary", base, "--", ".", *pathspecs],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def apply_patch(ws: Path, patch: str) -> None:
    if patch.strip():
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn"],
            cwd=ws,
            input=patch,
            text=True,
            check=True,
            capture_output=True,
        )
