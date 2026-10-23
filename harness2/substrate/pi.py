"""Pi Durable's editable instruction and skill surface."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ._native import export_files, native_tree, surface_files
from .base import Install

PI_SURFACE = SurfaceSpec(
    name="pi",
    always_on=("AGENTS.md",),
    always_on_max=8_000,
    always_on_cap_reason="Pi Durable host injects AGENTS.md on every model request",
    skill_dir=".agents/skills",
    skill_name_rule="overrides_dir",
    trigger_hint="",
    autotrigger_file="",
    trigger_line="",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    skills_need_trigger=False,
    script_dir=".agents/scripts",
    reject_empty_always_on=False,
)
NATIVE_PATHS = ("AGENTS.md", ".agents/skills", ".agents/scripts")


class PiSubstrate:
    name = "pi"
    NATIVE_PATHS = NATIVE_PATHS

    def surface(self) -> SurfaceSpec:
        return PI_SURFACE

    def import_tree(self, root: Path, *, rejected: list[str] | None = None) -> HT.Tree:
        return native_tree(PI_SURFACE, root, NATIVE_PATHS, rejected)

    def export_tree(self, tree: HT.Tree, root: Path, *, base: HT.Tree) -> dict[str, list[str]]:
        return export_files(root, surface_files(PI_SURFACE, tree), surface_files(PI_SURFACE, base))

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "pi_root":
            raise ValueError(f"Pi needs install_mode 'pi_root', got {spec.install_mode!r}")
        staged = Path(tempfile.mkdtemp(prefix="harness2-pi-root-"))
        try:
            self.export_tree(self.import_tree(tree_dir), staged, base={})
            return Install(staged, RM.fingerprint(tree_dir), {"install_mode": "pi_root"})
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def strip_native(self, workspace: Path) -> None:
        self.export_tree({}, workspace, base=self.import_tree(workspace))

    def install_into(self, install: Install, workspace: Path) -> None:
        self.export_tree(self.import_tree(install.staging_dir), workspace, base={})

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        rows = []
        for line in Path(stream_path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = json.dumps(entry.get("model") or entry.get("data") or {}, ensure_ascii=False)
            kind = entry.get("kind", "entry")
            rows.append(f"[OBSERVATION {kind}] {payload[-obs_trunc:]}")
        return "\n".join(rows)[-max_chars:]

    def final_message(self, stream_path: Path) -> str:
        path = Path(stream_path)
        result = path.with_name("result.json")
        if not result.is_file():
            result = path.parent / "pi-session/result.json"
        data = json.loads(result.read_text(encoding="utf-8"))
        return str(data.get("answer", ""))

    def cost_usd(self, stream_path: Path) -> float:
        return 0.0

    def preflight(self) -> None:
        pass
