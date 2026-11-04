import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import managed
from harness2.providers import Provider


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # No managed test can discover a real model CLI or inherit authentication.
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        for name in ("codex", "claude", "opencode"):
            binary = fake_bin / name
            binary.write_text(
                "#!/bin/sh\necho 'real CLI invocation forbidden in managed tests' >&2\nexit 73\n"
            )
            binary.chmod(0o755)
        for name in ("home", "codex-home", "config", "state"):
            (self.root / name).mkdir()
        environment = patch.dict(
            os.environ,
            {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(self.root / "home"),
                "CODEX_HOME": str(self.root / "codex-home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.codex = self.root / "codex.py"
        self.codex.write_text(
            "import json,sys; prompt=sys.stdin.read(); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'answer'}}))"
        )
        self.binary = f"{sys.executable} {self.codex}"
        self.runner = self.root / "runner.py"
        self.runner.write_text("""import json, pathlib, subprocess, sys
p = pathlib.Path(sys.argv[-1]); r=json.loads(p.read_text())
e=pathlib.Path(r['evidence_dir'])
if e.exists():
 print((e/'response').read_text()); sys.exit(0)
e.mkdir()
with (e.parent/'launch-count').open('a') as count: count.write('child\\n')
with open(r['stdin_file'],'rb') as stdin:
 child=subprocess.run(r['argv'],stdin=stdin,cwd=r['cwd'],capture_output=True)
(e/'stdout').write_bytes(child.stdout)
(e/'stderr').write_bytes(child.stderr)
response=json.dumps({'version':1,'key':r['key'],'request_digest':__import__('hashlib').sha256(json.dumps({k:v for k,v in r.items() if k!='key'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'phase':'finished','exit_code':child.returncode,'error':None,'artifacts':{'stdout':str(e/'stdout'),'stderr':str(e/'stderr')}})
(e/'response').write_text(response)
print(response)
""")
        self.context = self.root / "context.json"
        self.context.write_text(
            json.dumps(
                dict(
                    version=1,
                    objective="test",
                    acceptance=["works"],
                    repo_revision="abc",
                    harness_digest="0" * 64,
                    sources=[],
                    tools_available=[],
                    skills_available=[],
                )
            )
        )
        self.config = managed.ManagedConfig(
            (sys.executable, str(self.runner)), self.root / "db", None, "fixture", self.context
        )

    def invoke(self, **overrides):
        args = dict(
            config=self.config,
            slot=self.root / "slot",
            role="rollout",
            model="gpt-test",
            effort="high",
            prompt="task",
            harness_digest="1" * 64,
            provider=Provider("login", "chatgpt-login"),
            timeout=20,
            prepare=lambda ws: ws.mkdir(),
            binary=self.binary,
        )
        args.update(overrides)
        return managed.execute(**args)

    def test_preserves_request_key_and_workspace_on_repeat(self):
        first = self.invoke()
        request = (self.root / "slot/request.json").read_bytes()
        (first.workspace / "kept").write_text("durable")
        second = self.invoke()
        self.assertEqual(second.text, "answer")
        self.assertEqual(request, (self.root / "slot/request.json").read_bytes())
        self.assertEqual((second.workspace / "kept").read_text(), "durable")
        r = json.loads(request)
        self.assertIn("--sandbox", r["argv"])
        self.assertIn("--skip-git-repo-check", r["argv"])
        self.assertNotIn("--ignore-rules", r["argv"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", r["argv"])
        self.assertEqual(r["role"], "rollout")
        self.assertEqual(
            r["stdin_sha256"],
            __import__("hashlib").sha256(Path(r["stdin_file"]).read_bytes()).hexdigest(),
        )

    def test_changed_inputs_rejected_without_deletion(self):
        self.invoke()
        before = (self.root / "slot/request.json").read_bytes()
        with self.assertRaises(managed.ManagedError):
            self.invoke(prompt="changed")
        self.assertEqual(before, (self.root / "slot/request.json").read_bytes())

    def test_uncertain_stops_without_success(self):
        self.runner.write_text(
            self.runner.read_text().replace("'phase':'finished'", "'phase':'uncertain'")
        )
        with self.assertRaises(managed.ManagedError):
            self.invoke()
        self.assertFalse((self.root / "slot/complete.json").exists())

    def test_proposer_roles_preserve_workspace_and_reject_changed_inputs(self):
        from harness2.core import events
        from harness2.core.proposer import Proposer
        from harness2.core.spec import ChannelSpec

        for role in ("improver", "selector", "reflector"):
            prop = Proposer(
                ChannelSpec(
                    "codex",
                    "login/gpt-test",
                    provider=Provider("login", "chatgpt-login"),
                    managed=self.config,
                    bin=self.binary,
                    role=role,
                ),
                self.root / role,
            )
            first = prop.call("mission", None, role, harvest="output")
            self.assertTrue(first.ok)
            self.assertTrue(first.workspace.is_dir())
            self.assertEqual(prop.call("mission", None, role).text, "answer")
            r = json.loads(next((self.root / role).glob("*.managed/request.json")).read_text())
            self.assertEqual(r["role"], role)
            self.assertIn("--skip-git-repo-check", r["argv"])
            with self.assertRaises(events.Stopped):
                prop.call("changed", None, role)

    def test_rollout_preserves_slot_and_changed_inputs_fail_before_cache(self):
        from harness2 import registry, suite
        from harness2.core import events
        from harness2.settings import load

        project = self.root / "project"
        project.mkdir()
        (project / ".harness2").mkdir()
        overrides = {
            "executor.name": "codex",
            "executor.model": "chatgpt-login/gpt-test",
            "executor.bin": self.binary,
            "improver.channel": "codex",
            "improver.model": "chatgpt-login/gpt-test",
            "managed.runner": list(self.config.runner),
            "managed.db": str(self.config.db),
            "managed.scope_local": "fixture",
            "managed.context": str(self.context),
        }
        settings = load(project, overrides={k: (v, "test") for k, v in overrides.items()})
        suite.new_task(settings.path("paths.tasks"), "one", files=True, domain="test", check=None)
        adapter = suite.GenericAdapter(registry.SuiteContext(settings, project, project / "runs"))
        run = adapter.execute("one", None, "test")
        self.assertTrue((run / "workspace").is_dir())
        self.assertTrue((run / "meta.json").is_file())
        original_meta = (run / "meta.json").read_bytes()
        self.assertFalse(adapter.artifacts_intact(run))
        self.assertEqual(adapter.execute("one", None, "test"), run)
        self.assertEqual(original_meta, (run / "meta.json").read_bytes())
        before = (run / "request.json").read_bytes()
        adapter.executor["model"] = "chatgpt-login/changed-model"
        with self.assertRaises(events.Stopped):
            adapter.execute("one", None, "test")
        self.assertEqual(before, (run / "request.json").read_bytes())
        self.assertEqual((run / "launch-count").read_text(), "child\n")
        adapter.executor["model"] = "chatgpt-login/gpt-test"
        (settings.path("paths.tasks") / "one/task.md").write_text("changed")
        with self.assertRaises(events.Stopped):
            adapter.execute("one", None, "test")

    def test_concurrent_calls_reuse_one_runner_execution(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.invoke(), range(2)))
        self.assertEqual([r.text for r in results], ["answer", "answer"])
        self.assertEqual((self.root / "slot/launch-count").read_text(), "child\n")

    def test_failed_child_is_never_retried_or_completed(self):
        self.codex.write_text(self.codex.read_text() + "\nsys.exit(7)\n")
        for _ in range(2):
            with self.assertRaises(managed.ManagedError):
                self.invoke()
        self.assertEqual((self.root / "slot/launch-count").read_text(), "child\n")
        self.assertFalse((self.root / "slot/complete.json").exists())

    def test_runner_unavailable_preserves_slot(self):
        from dataclasses import replace

        bad = replace(self.config, runner=("/nonexistent-managed-runner",))
        with self.assertRaises(managed.ManagedError):
            self.invoke(config=bad)
        self.assertTrue((self.root / "slot/request.json").is_file())
        self.assertTrue((self.root / "slot/workspace").is_dir())
        self.assertFalse((self.root / "slot/evidence").exists())

    def test_config_rejects_unsupported_channels_and_suite(self):
        from harness2.settings import ConfigError, load

        base = {
            "executor.name": "codex",
            "improver.channel": "codex",
            "managed.runner": list(self.config.runner),
            "managed.db": str(self.config.db),
            "managed.context": str(self.context),
            "managed.scope_local": "test",
        }
        for key, value in [
            ("suite.name", "other"),
            ("executor.name", "claude"),
            ("selector.channel", "human"),
            ("reflector.channel", "pi"),
        ]:
            with self.subTest(key=key), self.assertRaises(ConfigError):
                load(None, overrides={k: (v, "test") for k, v in (base | {key: value}).items()})

    def test_rollout_manifest_uses_sha256_harness_digest(self):
        self.test_rollout_preserves_slot_and_changed_inputs_fail_before_cache()
        request = json.loads((self.root / "project/runs/rollouts/test/request.json").read_text())
        self.assertRegex(request["context_manifest"]["harness_digest"], r"^[0-9a-f]{64}$")

    def test_modified_runner_artifacts_block_replay(self):
        result = self.invoke()
        result.stdout.write_text(
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "tampered"}}
            )
            + "\n"
        )
        with self.assertRaises(managed.ManagedError):
            self.invoke()

    def test_custom_binary_cannot_add_bypass_flags(self):
        with self.assertRaises(managed.ManagedError):
            self.invoke(binary="codex --dangerously-bypass-approvals-and-sandbox")
        self.assertFalse((self.root / "slot/request.json").exists())

    def test_parent_and_host_identity_are_preserved(self):
        from unittest.mock import patch

        with patch.dict(
            "os.environ", {"MAYKY_RUN_ID": "parent-run", "MAYKY_WORK_HOST": "authorized-host"}
        ):
            self.invoke()
        request = json.loads((self.root / "slot/request.json").read_text())
        self.assertEqual(request["launched_by"], "parent-run")
        descriptor = json.loads((self.root / "slot/descriptor.json").read_text())
        self.assertTrue(descriptor["inputs_digest"])

    def test_finalizer_runs_under_slot_serialization(self):
        def finalize(result):
            import fcntl

            with (self.root / "slot/lock").open("a+b") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (result.workspace / "finalized").write_text(result.text)

        self.invoke(finalize=finalize)
        self.assertEqual((self.root / "slot/workspace/finalized").read_text(), "answer")

    def test_preexisting_slot_evidence_is_preserved(self):
        slot = self.root / "slot"
        slot.mkdir()
        (slot / "final.md").write_text("previous evidence")
        with self.assertRaises(managed.ManagedError):
            self.invoke()
        self.assertEqual((slot / "final.md").read_text(), "previous evidence")

    def test_harvest_source_mutation_rejects_replay_and_preserves_snapshot(self):
        from harness2.core import events
        from harness2.core.proposer import Proposer
        from harness2.core.spec import ChannelSpec

        for mutation in ("modify", "add"):
            with self.subTest(mutation=mutation):
                context = self.root / f"context-{mutation}"
                (context / "harness").mkdir(parents=True)
                (context / "harness/AGENTS.md").write_text("original")
                prop = Proposer(
                    ChannelSpec(
                        "codex",
                        "login/gpt-test",
                        provider=Provider("login", "chatgpt-login"),
                        managed=self.config,
                        bin=self.binary,
                    ),
                    self.root / f"audit-{mutation}",
                )
                reply = prop.call("mission", context, "improver", harvest="context/harness")
                slot = reply.workspace.parent
                source = slot / "workspace/context/harness"
                self.assertNotEqual(reply.workspace, source)
                target = source / ("AGENTS.md" if mutation == "modify" else "added.md")
                target.write_text("altered")
                with self.assertRaises(events.Stopped):
                    prop.call("mission", context, "improver", harvest="context/harness")
                self.assertEqual((reply.workspace / "AGENTS.md").read_text(), "original")
                self.assertFalse((reply.workspace / "added.md").exists())
                self.assertEqual(target.read_text(), "altered")
                self.assertEqual((slot / "launch-count").read_text(), "child\n")

    def test_harvest_snapshot_mutation_rejects_replay_without_rebuild(self):
        from harness2.core import events
        from harness2.core.proposer import Proposer
        from harness2.core.spec import ChannelSpec

        for mutation in ("modify", "add"):
            with self.subTest(mutation=mutation):
                context = self.root / f"context-{mutation}"
                (context / "harness").mkdir(parents=True)
                (context / "harness/AGENTS.md").write_text("original")
                prop = Proposer(
                    ChannelSpec(
                        "codex",
                        "login/gpt-test",
                        provider=Provider("login", "chatgpt-login"),
                        managed=self.config,
                        bin=self.binary,
                    ),
                    self.root / f"audit-{mutation}",
                )
                reply = prop.call("mission", context, "improver", harvest="context/harness")
                target = reply.workspace / ("AGENTS.md" if mutation == "modify" else "added.md")
                target.write_text("altered")
                with self.assertRaises(events.Stopped):
                    prop.call("mission", context, "improver", harvest="context/harness")
                self.assertEqual(target.read_text(), "altered")
                self.assertEqual(
                    (reply.workspace.parent / "workspace/context/harness/AGENTS.md").read_text(),
                    "original",
                )

    def test_exit_zero_after_timeout_or_signal_never_completes(self):
        for reason in ("timeout", "signal:15"):
            with self.subTest(reason=reason):
                runner = self.root / f"runner-{reason.replace(':', '_')}.py"
                runner.write_text(
                    self.runner.read_text().replace(
                        "'exit_code':child.returncode",
                        f"'termination_reason':{reason!r},'exit_code':child.returncode",
                    )
                )
                from dataclasses import replace

                with self.assertRaises(managed.ManagedError):
                    self.invoke(
                        config=replace(self.config, runner=(sys.executable, str(runner))),
                        slot=self.root / reason.replace(":", "_"),
                    )
                self.assertFalse(
                    (self.root / reason.replace(":", "_") / "completion.json").exists()
                )

    def test_rollout_new_output_file_rejects_replay_without_relaunch(self):
        from harness2 import registry, suite
        from harness2.core import events
        from harness2.settings import load

        project = self.root / "project"
        project.mkdir()
        (project / ".harness2").mkdir()
        overrides = {
            "executor.name": "codex",
            "executor.model": "chatgpt-login/gpt-test",
            "executor.bin": self.binary,
            "improver.channel": "codex",
            "improver.model": "chatgpt-login/gpt-test",
            "managed.runner": list(self.config.runner),
            "managed.db": str(self.config.db),
            "managed.scope_local": "fixture",
            "managed.context": str(self.context),
        }
        settings = load(
            project, overrides={key: (value, "test") for key, value in overrides.items()}
        )
        suite.new_task(settings.path("paths.tasks"), "one", files=True, domain="test", check=None)
        adapter = suite.GenericAdapter(registry.SuiteContext(settings, project, project / "runs"))
        run = adapter.execute("one", None, "test")
        before = (run / "request.json").read_bytes()
        (run / "output/added.md").write_text("unexpected")
        with self.assertRaises(events.Stopped):
            adapter.execute("one", None, "test")
        self.assertEqual((run / "output/added.md").read_text(), "unexpected")
        self.assertEqual((run / "request.json").read_bytes(), before)
        self.assertEqual((run / "launch-count").read_text(), "child\n")
