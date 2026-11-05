"""Pi workspaces survive interrupted rollouts and model calls."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import registry, suite
from harness2.cli.main import main
from harness2.core import events, recursion, runmark
from harness2.core.judge import judge_one, judge_selections
from harness2.core.proposer import Proposer
from harness2.core.spec import ChannelSpec, RunView
from harness2.pi_runtime import PiRecoverable, PiResult, pi_session_lock
from harness2.settings import load


class PiWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".harness2/tasks").mkdir(parents=True)
        task = suite.new_task(
            self.project / ".harness2/tasks", "one", files=True, domain="default", check=None
        )
        (task / "task.md").write_text("Write output/result.txt")
        (task / "files/source.txt").write_text("source")
        (self.project / "AGENTS.md").write_text("Read the files")
        overrides = {
            "executor.name": ("pi", "test"),
            "executor.model": ("faux/faux", "test"),
            "improver.channel": ("pi", "test"),
            "improver.model": ("faux/faux", "test"),
        }
        settings = load(self.project, overrides=overrides)
        self.adapter = suite.make_adapter(
            registry.SuiteContext(settings, self.project, self.project / ".harness2/runs/test")
        )
        events.start(None)
        self.addCleanup(events.start, None)

    def test_cli_completes_pi_optimization_and_consolidation(self):
        """Exercise the prompt/recursion boundary, replacing only model transport."""
        (self.project / ".harness2/config.toml").write_text(
            '[executor]\nname = "pi"\nmodel = "faux/faux"\n'
            '[improver]\nchannel = "pi"\nmodel = "faux/faux"\n'
            "[run]\nphase_width = 1\nproposer_concurrency = 1\n"
        )

        def host(**kwargs):
            session = kwargs["session_dir"]
            session.mkdir(parents=True, exist_ok=True)
            trajectory = session / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant","model":[]}\n')
            if kwargs["slot"].startswith("rollout/"):
                (kwargs["cwd"] / "output/result.txt").write_text("delivered")
            answer = (
                "<notes>Keep the supported instructions unchanged.</notes>\n"
                "<experience>No evidence supporting a harness change.</experience>"
            )
            return PiResult(answer, 8, "request", session, trajectory)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
            patch("harness2.suite.run_pi_session", side_effect=host),
            patch("harness2.core.proposer.run_pi_session", side_effect=host),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["-C", str(self.project), "--json", "run", "--name", "full"])
        self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["result"]["domains"][0]["done"], 1)
        self.assertEqual(result["totals"]["calls"], 9)
        final = Path(result["final"]["parallel"]) / "AGENTS.md"
        self.assertEqual(final.read_text(), "Read the files")

    def test_rollout_resumes_the_mutated_workspace_without_completion_marker(self):
        calls = []
        run_dir = self.adapter.spec.runs_root / "rollouts/rollout-1"
        partial = run_dir / "workspace"
        partial.mkdir(parents=True)
        (partial / "source.txt").write_text("partial copy")

        def host(**kwargs):
            cwd = kwargs["cwd"]
            calls.append(cwd)
            if len(calls) == 1:
                self.assertEqual((cwd / "source.txt").read_text(), "source")
                (cwd / "output/result.txt").write_text("made before crash")
                raise PiRecoverable("host killed")
            self.assertEqual((cwd / "output/result.txt").read_text(), "made before crash")
            trajectory = kwargs["session_dir"] / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            return PiResult("done", 7, "request", kwargs["session_dir"], trajectory)

        with patch("harness2.suite.run_pi_session", side_effect=host):
            with self.assertRaises(events.Stopped):
                self.adapter.execute("one", None, "rollout-1")
            self.assertFalse(runmark.is_complete(run_dir))
            self.assertFalse((run_dir / "meta.json").exists())
            self.adapter.execute("one", None, "rollout-1")
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(runmark.is_complete(run_dir, harness_dir=self.adapter.base_harness()))
        self.assertEqual((run_dir / "output/result.txt").read_text(), "made before crash")
        (self.project / ".harness2/tasks/one/task.md").write_text("Changed task")
        with self.assertRaises(events.Stopped):
            self.adapter.execute("one", None, "rollout-1")
        (self.project / ".harness2/tasks/one/task.md").write_text("Write output/result.txt")
        (self.project / ".harness2/tasks/one/files/source.txt").write_text("changed source")
        with self.assertRaises(events.Stopped):
            self.adapter.execute("one", None, "rollout-1")
        (self.project / ".harness2/tasks/one/files/source.txt").write_text("source")
        self.adapter.executor["model"] = "faux/other"
        with self.assertRaises(events.Stopped):
            self.adapter.execute("one", None, "rollout-1")

    def test_unreadable_rollout_identity_stops_before_reusing_workspace(self):
        run_dir = self.adapter.spec.runs_root / "rollouts/broken"
        run_dir.mkdir(parents=True)
        (run_dir / "pi-prepared.json").write_text("{")
        with self.assertRaises(events.Stopped):
            self.adapter.execute("one", None, "broken")
        self.assertFalse((run_dir / "workspace").exists())

    def test_campaign_cache_checks_pi_identity_before_adoption(self):
        def host(**kwargs):
            session = kwargs["session_dir"]
            (session / "result.json").write_text('{"status":"done"}')
            trajectory = session / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            (kwargs["cwd"] / "output/result.txt").write_text("done")
            return PiResult("done", 3, "request", session, trajectory)

        with patch("harness2.suite.run_pi_session", side_effect=host):
            run_dir = self.adapter.execute("one", None, "cached")
        self.assertTrue(self.adapter.artifacts_intact(run_dir))
        work_dir = self.root / "campaign"
        rv = RunView("base", "cached", run_dir, True, True)
        record = vars(rv) | {"run_dir": runmark.relative_path(run_dir, self.adapter.spec.runs_root)}
        runmark.mark_complete(
            work_dir / "rollouts/base", run_id="cached", extra={"rollout": record}
        )
        self.assertIsNotNone(
            recursion._cached_rollout(self.adapter, work_dir, "base", "cached", None)
        )
        (self.project / ".harness2/tasks/one/files/source.txt").write_text("changed")
        self.assertFalse(self.adapter.artifacts_intact(run_dir))
        self.assertIsNone(recursion._cached_rollout(self.adapter, work_dir, "base", "cached", None))
        (self.project / ".harness2/tasks/one/files/source.txt").write_text("source")
        (run_dir / "pi-session/unsafe-effect.json").write_text('{"kind":"exec"}')
        with self.assertRaises(events.Stopped):
            self.adapter.execute("one", None, "cached")

    def test_proposer_resumes_same_working_directory_and_rejects_changed_call(self):
        audit = self.root / "audit"
        source = self.root / "context"
        source.mkdir()
        (source / "evidence.md").write_text("unchanged")
        calls = []

        def host(**kwargs):
            cwd = kwargs["cwd"]
            calls.append(cwd)
            if len(calls) == 1:
                (cwd / "work-in-progress").write_text("kept")
                raise PiRecoverable("host killed")
            self.assertEqual((cwd / "work-in-progress").read_text(), "kept")
            self.assertEqual((cwd / "context/evidence.md").read_text(), "unchanged")
            trajectory = kwargs["session_dir"] / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            return PiResult("answer", 8, "request", kwargs["session_dir"], trajectory)

        proposer = Proposer(ChannelSpec("pi", "faux/faux"), audit_dir=audit)
        with patch("harness2.core.proposer.run_pi_session", side_effect=host):
            with self.assertRaises(events.Stopped):
                proposer.call("mission", source, "improver")
            (calls[0] / "context-ready.json").unlink()
            (calls[0] / "context/evidence.md").write_text("partial copy")
            self.assertEqual(proposer.call("mission", source, "improver").text, "answer")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(proposer.call("mission", source, "improver").text, "answer")
        self.assertEqual(len(calls), 2)
        self.assertEqual(events.totals()["calls"], 1)
        (calls[0] / "pi-session/unsafe-effect.json").write_text('{"kind":"exec"}')
        with self.assertRaises(events.Stopped):
            proposer.call("mission", source, "improver")

    def test_project_rollout_keeps_worktree_and_exports_patch_after_recovery(self):
        task = suite.new_task(
            self.project / ".harness2/tasks", "code", files=False, domain="default", check=None
        )
        (task / "task.md").write_text("Change code.txt")
        (self.project / "code.txt").write_text("before\n")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.project, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@localhost",
                "commit",
                "-qm",
                "base",
            ],
            cwd=self.project,
            check=True,
        )
        partial = self.adapter.spec.runs_root / "rollouts/project-1/workspace"
        partial.mkdir(parents=True)
        (partial / "code.txt").write_text("partial worktree")
        calls = []

        def host(**kwargs):
            cwd = kwargs["cwd"]
            calls.append(cwd)
            if len(calls) == 1:
                (cwd / "code.txt").write_text("after\n")
                raise PiRecoverable("killed")
            self.assertEqual((cwd / "code.txt").read_text(), "after\n")
            trajectory = kwargs["session_dir"] / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            return PiResult("done", 9, "request", kwargs["session_dir"], trajectory)

        with patch("harness2.suite.run_pi_session", side_effect=host):
            with self.assertRaises(events.Stopped):
                self.adapter.execute("code", None, "project-1")
            result = self.adapter.execute("code", None, "project-1")
        self.assertEqual(calls[0], calls[1])
        self.assertIn("+after", (result / "deliverable.patch").read_text())
        self.assertEqual((self.project / "code.txt").read_text(), "before\n")

    def test_changed_proposer_inputs_cannot_overtake_unfinished_call(self):
        audit = self.root / "audit"
        proposer = Proposer(ChannelSpec("pi", "faux/faux"), audit_dir=audit)
        with patch("harness2.core.proposer.run_pi_session", side_effect=PiRecoverable("killed")):
            with self.assertRaises(events.Stopped):
                proposer.call("original", None, "selector")
        working = next(audit.glob("selector.*.working"))
        session = working / "pi-session"
        (session / "request.json").write_text("{}")
        with self.assertRaises(events.Stopped):
            proposer.call("changed", None, "selector")

    def test_busy_pi_rollout_and_judge_stop_the_campaign(self):
        session = self.adapter.spec.runs_root / "rollouts/blocked/pi-session"
        with pi_session_lock(session):
            with self.assertRaises(events.Stopped):
                self.adapter.execute("one", None, "blocked")
        failing = SimpleNamespace(
            score=lambda _run, _task: (_ for _ in ()).throw(events.Stopped("recoverable Pi")),
            spec=SimpleNamespace(runs_root=self.root),
        )
        with self.assertRaises(events.Stopped):
            judge_one(failing, "one", "parallel", 1, "chosen", "id", self.root)
        selections = [
            {
                "ok": True,
                "chosen_label": "chosen",
                "chosen_run_dir": ".",
                "task_id": "one",
                "mode": "parallel",
                "k": 1,
                "chosen_run_id": "id",
            }
        ]
        with self.assertRaises(events.Stopped):
            judge_selections(failing, selections, parallel=1)
