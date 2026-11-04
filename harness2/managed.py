"""Opt-in Codex execution through the versioned managed-runner protocol.

Slots are persistent. A repeated invocation consults the runner with the original
request; it never creates a fresh execution identity or retries a failed child.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .providers import Provider, codex_login_home
from .settings import ConfigError


class ManagedError(RuntimeError):
    """A managed slot needs operator attention; automatic retries are forbidden."""


def encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def tree_digest(root: Path | None) -> str:
    entries = []
    if root is not None:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                entries.append(
                    [str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()]
                )
    return digest(entries)


def harvest_inventory(root: Path) -> list[dict]:
    """Record every path and byte in a self-contained harvested tree."""
    if not root.is_dir() or root.is_symlink():
        raise ManagedError(f"managed harvest directory missing or linked: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            raise ManagedError(f"managed harvest contains a symbolic link: {path}")
        if path.is_dir():
            entries.append({"path": name, "type": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": name,
                    "type": "file",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            raise ManagedError(f"managed harvest contains a special file: {path}")
    return entries


def harvest_snapshot(slot: Path, workspace: Path, requested: str | None) -> Path | None:
    """Create once and validate both the source and immutable-by-contract snapshot.

    A changed or incomplete snapshot is evidence requiring operator attention;
    it is never silently replaced from the mutable session workspace.
    """
    snapshot = slot / "harvest"
    marker = slot / "harvest.json"
    try:
        if marker.exists():
            stored = json.loads(marker.read_text())
            name = stored["source"]
            if requested is not None and requested != name:
                raise ManagedError(f"managed harvest path changed: {slot}")
            source = workspace / name
            if not source.resolve().is_relative_to(workspace.resolve()):
                raise ManagedError(f"managed harvest source escapes workspace: {source}")
            expected = stored["inventory"]
            if harvest_inventory(snapshot) != expected or harvest_inventory(source) != expected:
                raise ManagedError(f"managed harvest source or snapshot changed: {slot}")
            return snapshot if requested is not None else None
        if snapshot.exists():
            raise ManagedError(f"incomplete managed harvest snapshot: {snapshot}")
        if requested is None:
            return None
        if (slot / "completion.json").exists():
            raise ManagedError(f"completed managed call has no harvest snapshot: {slot}")
        relative = Path(requested)
        source = workspace / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not source.resolve().is_relative_to(workspace.resolve())
        ):
            raise ManagedError(f"managed harvest must remain inside workspace: {requested}")
        inventory = harvest_inventory(source)
        shutil.copytree(source, snapshot)
        if harvest_inventory(snapshot) != inventory or harvest_inventory(source) != inventory:
            raise ManagedError(f"managed harvest changed during snapshot: {slot}")
        # Sync copied bytes and directory entries before publishing the inventory.
        directories = [snapshot]
        for path in snapshot.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            elif path.is_dir():
                directories.append(path)
        for path in reversed(directories):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        atomic(marker, {"source": requested, "inventory": inventory})
        return snapshot
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManagedError(f"managed harvest cannot be verified: {slot}: {exc}") from exc


def codex_binary(binary: str) -> tuple[list[str], list[dict]]:
    argv = shlex.split(binary) if binary else ["codex"]
    if not argv or len(argv) > 2 or any(v.startswith("-") for v in argv):
        raise ManagedError(
            "managed bin must be an executable, optionally with one script path; flags are forbidden"
        )
    if len(argv) == 2 and not Path(argv[1]).is_file():
        raise ManagedError("managed bin second argument must be an existing script file")
    identity = []
    for value in argv:
        location = shutil.which(value) or value
        path = Path(location).resolve()
        identity.append(
            {
                "configured": value,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            }
        )
    return argv, identity


def atomic(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as out:
        out.write(encoded(value))
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_evidence(files: list[Path], trees: list[Path]) -> None:
    """Sync file contents and directory entries before a completion marker."""
    all_files = set(files)
    directories = {path.parent for path in files}
    for root in trees:
        if not root.exists():
            continue
        directories.add(root)
        for path in root.rglob("*"):
            if path.is_file():
                all_files.add(path)
                directories.add(path.parent)
            elif path.is_dir():
                directories.add(path)
    for path in sorted(all_files):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def completed(slot: Path, files: list[Path] | None = None, trees: list[Path] | None = None) -> bool:
    """Seal full inventories, including additions, after syncing all final evidence."""
    marker = slot / "completion.json"

    def inventory(path):
        return harvest_inventory(path) if path.exists() or path.is_symlink() else None

    try:
        if marker.exists():
            stored = json.loads(marker.read_text())
            for name, expected in stored["files"].items():
                if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
                    raise ManagedError(f"completed managed artifact changed: {name}")
            for name, expected in stored["trees"].items():
                if inventory(Path(name)) != expected:
                    raise ManagedError(f"completed managed artifact inventory changed: {name}")
            return True
        if files is not None:
            trees = trees or []
            stored = {
                "files": {
                    str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in files
                },
                "trees": {str(path.resolve()): inventory(path) for path in trees},
            }
            sync_evidence(files, trees)
            for name, expected in stored["files"].items():
                if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
                    raise ManagedError(f"managed evidence changed before completion: {name}")
            for name, expected in stored["trees"].items():
                if inventory(Path(name)) != expected:
                    raise ManagedError(f"managed inventory changed before completion: {name}")
            atomic(marker, stored)
        return False
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManagedError(f"managed completion cannot be verified: {slot}") from exc


@dataclass(frozen=True)
class ManagedConfig:
    runner: tuple[str, ...]
    db: Path
    hilo: str | None
    scope_local: str | None
    context: Path
    sandbox: str = "workspace-write"

    @classmethod
    def from_settings(cls, settings):
        values = settings.get("managed", {})
        if not values:
            return None
        runner = values.get("runner")
        if (
            not isinstance(runner, list)
            or not runner
            or not all(isinstance(v, str) and v for v in runner)
        ):
            raise ConfigError("managed.runner must be a nonempty argv list")
        for key in ("db", "context"):
            if not isinstance(values.get(key), str) or not Path(values[key]).is_absolute():
                raise ConfigError(f"managed.{key} must be an absolute path")
        hilo, scope = values.get("hilo"), values.get("scope_local")
        if (hilo is None) == (scope is None):
            raise ConfigError("managed requires exactly one of hilo or scope_local")
        if any(v is not None and (not isinstance(v, str) or not v) for v in (hilo, scope)):
            raise ConfigError("managed hilo/scope_local must be nonempty strings")
        sandbox = values.get("sandbox", "workspace-write")
        if sandbox not in ("workspace-write", "read-only"):
            raise ConfigError("managed.sandbox must be workspace-write or read-only")
        if settings.get("suite.name") != "tasks" or settings.get("executor.name") != "codex":
            raise ConfigError("managed supports only the tasks suite and Codex executor")
        for role in ("improver", "selector", "reflector"):
            if settings.role(role)["channel"] != "codex":
                raise ConfigError(f"managed {role} requires the Codex channel")
        return cls(tuple(runner), Path(values["db"]), hilo, scope, Path(values["context"]), sandbox)


@dataclass(frozen=True)
class ManagedResult:
    workspace: Path
    stdout: Path
    stderr: Path
    text: str
    response: dict


@contextlib.contextmanager
def slot_lock(slot: Path):
    slot.mkdir(parents=True, exist_ok=True)
    with (slot / "lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def execute(
    *,
    config: ManagedConfig,
    slot: Path,
    role: str,
    model: str,
    effort: str,
    prompt: str,
    harness_digest: str,
    provider: Provider | None,
    timeout: int,
    prepare: Callable[[Path], None],
    binary: str = "",
    inputs: object = None,
    finalize: Callable[[ManagedResult], None] | None = None,
) -> ManagedResult:
    """Serialize preparation, runner consultation, harvest and completion in one slot."""
    slot = slot.resolve()
    with slot_lock(slot):
        result = _execute(
            config=config,
            slot=slot,
            role=role,
            model=model,
            effort=effort,
            prompt=prompt,
            harness_digest=harness_digest,
            provider=provider,
            timeout=timeout,
            prepare=prepare,
            binary=binary,
            inputs=inputs,
        )
        if finalize is not None:
            finalize(result)
        return result


def _execute(
    *,
    config,
    slot,
    role,
    model,
    effort,
    prompt,
    harness_digest,
    provider,
    timeout,
    prepare,
    binary,
    inputs,
):
    if role not in ("rollout", "improver", "selector", "reflector"):
        raise ManagedError("managed judge/unknown role is unsupported")
    if provider is not None and provider.type not in ("chatgpt-login", "openai"):
        raise ManagedError("managed Codex requires a configured Codex provider")
    try:
        manifest = json.loads(config.context.read_text(encoding="utf-8"))
        required = {
            "version",
            "objective",
            "acceptance",
            "repo_revision",
            "harness_digest",
            "sources",
            "tools_available",
            "skills_available",
        }
        if (
            not isinstance(manifest, dict)
            or not required <= manifest.keys()
            or manifest["version"] != 1
        ):
            raise ValueError("missing ContextManifest v1 fields")
    except (OSError, ValueError) as exc:
        raise ManagedError(f"cannot materialize managed context: {exc}") from exc
    provider_info = asdict(provider) if provider else None
    manifest["harness_digest"] = harness_digest
    manifest["provider"] = provider_info or {}
    manifest["materialized_digest"] = digest(manifest)
    orientation = (
        "# Managed context\nThe following manifest records available context, not proof "
        "that any source or skill was loaded. Read applicable repository instructions "
        "and sources before acting.\n" + encoded(manifest).decode() + "\n\n"
    )
    prompt_bytes = (orientation + prompt).encode()
    ws = slot / "workspace"
    executable, binary_identity = codex_binary(binary)
    argv = [
        *executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        config.sandbox,
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={json.dumps(effort)}",
    ]
    if provider and provider.type == "openai":
        pid = "harness2-" + provider.name
        for value in (
            f"model_provider={json.dumps(pid)}",
            f"model_providers.{pid}.name={json.dumps(provider.name)}",
            f"model_providers.{pid}.base_url={json.dumps(provider.base_url)}",
            f"model_providers.{pid}.env_key={json.dumps(provider.api_key_env)}",
            f'model_providers.{pid}.wire_api="responses"',
        ):
            argv.extend(["-c", value])
    argv.extend(["-C", str(ws), "-"])
    identity = dict(
        model=model,
        codex_binary=binary_identity,
        launched_by=os.environ.get("MAYKY_RUN_ID"),
        effort=effort,
        prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
        manifest=manifest,
        argv=argv,
        timeout=timeout,
        inputs=inputs,
        runner=list(config.runner),
        db=str(config.db),
        hilo=config.hilo,
        scope_local=config.scope_local,
        host=os.environ.get("MAYKY_WORK_HOST"),
        codex_home=str(codex_login_home(provider))
        if provider and provider.type == "chatgpt-login"
        else os.environ.get("CODEX_HOME"),
    )
    identity_digest = digest(identity)
    descriptor = slot / "descriptor.json"
    request_path = slot / "request.json"
    if descriptor.exists():
        try:
            saved = json.loads(descriptor.read_text())
            if saved["inputs_digest"] != identity_digest or not ws.is_dir():
                raise ManagedError(
                    f"managed inputs changed or workspace missing at {slot}; use a new run/name"
                )
            request = json.loads(request_path.read_text())
            if (
                digest({k: v for k, v in request.items() if k != "key"}) != saved["request_digest"]
                or request["key"] != saved["key"]
            ):
                raise ManagedError(f"managed request changed at {slot}")
            if (slot / "prompt.md").read_bytes() != prompt_bytes:
                raise ManagedError(f"managed prompt changed at {slot}")
        except (OSError, ValueError, KeyError) as exc:
            raise ManagedError(f"incomplete managed descriptor at {slot}: {exc}") from exc
    else:
        if any(path.name != "lock" for path in slot.iterdir()):
            raise ManagedError(f"unprepared managed slot at {slot}; inspect before recovery")
        prepare(ws)
        (slot / "prompt.md").write_bytes(prompt_bytes)
        atomic(slot / "context.json", manifest)
        request = dict(
            version=1,
            key="h2-" + uuid.uuid4().hex,
            hilo=config.hilo,
            scope_local=config.scope_local,
            launched_by=os.environ.get("MAYKY_RUN_ID"),
            retry_of=None,
            harness="codex",
            modo="harness" if role == "rollout" else "subagente",
            role=role,
            model=model,
            effort=effort,
            cwd=str(ws),
            argv=argv,
            stdin_file=str(slot / "prompt.md"),
            stdin_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
            evidence_dir=str(slot / "evidence"),
            timeout_s=timeout,
            context_manifest=manifest,
        )
        atomic(request_path, request)
        atomic(
            descriptor,
            dict(
                inputs_digest=identity_digest,
                key=request["key"],
                request_digest=digest({k: v for k, v in request.items() if k != "key"}),
            ),
        )
    env = dict(os.environ)
    if provider and provider.type == "chatgpt-login":
        env["CODEX_HOME"] = str(codex_login_home(provider))
    try:
        result = subprocess.run(
            [
                *config.runner,
                "--db",
                str(config.db),
                "managed-exec",
                "--request",
                str(request_path),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("runner response must be an object")
        atomic(slot / "response.json", response)
    except (OSError, ValueError) as exc:
        raise ManagedError(f"managed runner unavailable/invalid; preserve {slot}: {exc}") from exc
    expected = digest({k: v for k, v in request.items() if k != "key"})
    if (
        result.returncode
        or response.get("version") != 1
        or response.get("key") != request["key"]
        or response.get("request_digest") != expected
    ):
        raise ManagedError(
            f"managed runner rejected request or returned invalid identity: {response.get('error')}"
        )
    if (
        response.get("phase") != "finished"
        or response.get("exit_code") != 0
        or response.get("termination_reason") is not None
        or response.get("error")
    ):
        raise ManagedError(
            f"managed execution stopped ({response.get('phase')}, exit {response.get('exit_code')}): {response.get('error')}; evidence: {slot}"
        )
    try:
        stdout = Path(response["artifacts"]["stdout"])
        stderr = Path(response["artifacts"]["stderr"])
        stream = stdout.read_text(encoding="utf-8")
        stderr.read_bytes()
        text = ""
        for line in stream.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Codex event must be an object")
            if event.get("type") in ("turn.failed", "error"):
                raise ManagedError(f"Codex stream reports failure; evidence: {slot}")
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = str(item.get("text") or "")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManagedError(f"managed artifacts unavailable: {exc}") from exc
    if not text.strip():
        raise ManagedError(f"managed execution has no final answer; evidence: {slot}")
    hashes = {
        "stdout": hashlib.sha256(stdout.read_bytes()).hexdigest(),
        "stderr": hashlib.sha256(stderr.read_bytes()).hexdigest(),
    }
    integrity = slot / "artifacts.json"
    if integrity.exists():
        try:
            if json.loads(integrity.read_text()) != hashes:
                raise ManagedError(f"managed artifacts changed: {slot}")
        except (OSError, ValueError) as exc:
            raise ManagedError(f"unreadable managed artifact hashes: {slot}") from exc
    else:
        atomic(integrity, hashes)
    return ManagedResult(ws, stdout, stderr, text, response)
