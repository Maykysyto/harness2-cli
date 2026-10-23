"""Stable, single-owner bridge from Harness² calls to the Pi Durable host."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .settings import ConfigError

RUNTIME_VERSION = "1.4.0"
HOST = Path(__file__).resolve().parents[1] / "pi-runtime" / "host.mjs"


class PiBusy(RuntimeError):
    """Another process owns this Pi Durable database."""


class PiDescriptorMismatch(RuntimeError):
    """A call tried to reuse a Durable session with different immutable inputs."""


class PiRecoverable(RuntimeError):
    """The Pi call has not produced a final answer; reissue the same run to resume."""


@dataclass(frozen=True)
class PiResult:
    answer: str
    submission_id: int
    request_id: str
    session_dir: Path
    trajectory: Path


@contextlib.contextmanager
def pi_session_lock(session_dir: Path) -> Iterator[int]:
    """Own a session through export; pass this fd to the child to survive parent death."""
    session_dir = Path(session_dir).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "owner.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PiBusy(f"Pi session is owned by another process: {session_dir}") from exc
        yield fd
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as output:
        os.chmod(tmp, 0o600)
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)


def _descriptor(
    *,
    slot: str,
    session_dir: Path,
    cwd: Path,
    model: str,
    effort: str,
    system: str,
    prompt: str,
    context_digest: str,
    harness_digest: str,
) -> dict:
    if not slot or not model or "/" not in model or not prompt:
        raise ConfigError("Pi call needs a slot, provider/model and non-empty prompt")
    identity = {
        "slot": slot,
        "model": model,
        "effort": effort,
        "system": system,
        "prompt": prompt,
        "contextDigest": context_digest,
        "harnessDigest": harness_digest,
        "runtimeVersion": RUNTIME_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        **identity,
        "fingerprint": fingerprint,
        "requestId": f"h2-{fingerprint[:32]}",
        "sessionDir": str(session_dir.resolve()),
        "cwd": str(cwd.resolve()),
    }


def _result(session_dir: Path, descriptor: dict) -> PiResult | None:
    path = session_dir / "result.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PiRecoverable(f"Pi result is unreadable in {session_dir}: {exc}") from exc
    if (
        data.get("fingerprint") != descriptor["fingerprint"]
        or data.get("requestId") != descriptor["requestId"]
    ):
        raise PiDescriptorMismatch(f"Pi result does not match request in {session_dir}")
    if data.get("status") != "done" or not str(data.get("answer", "")).strip():
        raise PiRecoverable(f"Pi has no usable final answer in {session_dir}")
    trajectory = session_dir / "trajectory.jsonl"
    if not trajectory.is_file():
        raise PiRecoverable(f"Pi result has no trajectory in {session_dir}")
    return PiResult(
        str(data["answer"]),
        int(data["submissionId"]),
        descriptor["requestId"],
        session_dir,
        trajectory,
    )


def run_pi_session(
    *,
    slot: str,
    session_dir: Path,
    cwd: Path,
    model: str,
    effort: str,
    system: str,
    prompt: str,
    context_digest: str = "",
    harness_digest: str = "",
    node_bin: str = "node",
    timeout: int = 3600,
    agent_dir: Path | None = None,
    lock_fd: int | None = None,
) -> PiResult:
    """Start or resume exactly one Pi submission, preserving its database on errors."""
    session_dir = Path(session_dir).resolve()
    lock = contextlib.nullcontext(lock_fd) if lock_fd is not None else pi_session_lock(session_dir)
    with lock as owned_fd:
        descriptor = _descriptor(
            slot=slot,
            session_dir=session_dir,
            cwd=Path(cwd),
            model=model,
            effort=effort,
            system=system,
            prompt=prompt,
            context_digest=context_digest,
            harness_digest=harness_digest,
        )
        path = session_dir / "request.json"
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PiDescriptorMismatch(f"Pi request is unreadable in {session_dir}") from exc
            if existing != descriptor:
                raise PiDescriptorMismatch(
                    f"Pi request changed for {descriptor['slot']} in {session_dir}"
                )
        else:
            if (session_dir / "session.sqlite").exists():
                raise PiDescriptorMismatch("Pi database exists without its request descriptor")
            _atomic_json(path, descriptor)
        if previous := _result(session_dir, descriptor):
            return previous
        env = dict(os.environ)
        if agent_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(Path(agent_dir).resolve())
        try:
            process = subprocess.run(
                [node_bin, str(HOST), str(path)],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                pass_fds=(owned_fd,),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            if completed := _result(session_dir, descriptor):
                return completed
            raise PiRecoverable(f"Pi host interrupted for {descriptor['slot']}: {exc}") from exc
        (session_dir / "host-stderr.log").write_text(process.stderr[-16000:], encoding="utf-8")
        if completed := _result(session_dir, descriptor):
            return completed
        raise PiRecoverable(
            f"Pi host did not complete {descriptor['slot']} (exit {process.returncode}); "
            f"see {session_dir / 'host-stderr.log'}"
        )
