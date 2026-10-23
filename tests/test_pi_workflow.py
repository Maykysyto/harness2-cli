"""Pi workspaces survive interrupted rollouts and model calls."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import registry, suite
from harness2.core import events, runmark
from harness2.core.proposer import Proposer
from harness2.core.spec import ChannelSpec
from harness2.pi_runtime import PiDescriptorMismatch, PiRecoverable, PiResult
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

    def test_rollout_resumes_the_mutated_workspace_without_completion_marker(self):
        calls = []

        def host(**kwargs):
            cwd = kwargs["cwd"]
            calls.append(cwd)
            if len(calls) == 1:
                (cwd / "output/result.txt").write_text("made before crash")
                raise PiRecoverable("host killed")
            self.assertEqual((cwd / "output/result.txt").read_text(), "made before crash")
            trajectory = kwargs["session_dir"] / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            return PiResult("done", 7, "request", kwargs["session_dir"], trajectory)

        with patch("harness2.suite.run_pi_session", side_effect=host):
            with self.assertRaises(events.Stopped):
                self.adapter.execute("one", None, "rollout-1")
            run_dir = self.adapter.spec.runs_root / "rollouts/rollout-1"
            self.assertFalse(runmark.is_complete(run_dir))
            self.assertFalse((run_dir / "meta.json").exists())
            self.adapter.execute("one", None, "rollout-1")
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(runmark.is_complete(run_dir, harness_dir=self.adapter.base_harness()))
        self.assertEqual((run_dir / "output/result.txt").read_text(), "made before crash")

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
            trajectory = kwargs["session_dir"] / "trajectory.jsonl"
            trajectory.write_text('{"kind":"pi.assistant"}\n')
            return PiResult("answer", 8, "request", kwargs["session_dir"], trajectory)

        proposer = Proposer(ChannelSpec("pi", "faux/faux"), audit_dir=audit)
        with patch("harness2.core.proposer.run_pi_session", side_effect=host):
            with self.assertRaises(events.Stopped):
                proposer.call("mission", source, "improver")
            self.assertEqual(proposer.call("mission", source, "improver").text, "answer")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(proposer.call("mission", source, "improver").text, "answer")
        self.assertEqual(len(calls), 2)

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
        with self.assertRaises(PiDescriptorMismatch):
            proposer.call("changed", None, "selector")
