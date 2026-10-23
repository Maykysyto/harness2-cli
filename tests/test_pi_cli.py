"""Pi opt-in setup and local runtime diagnostics."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.cli import main
from harness2.pi_runtime import pi_preflight
from harness2.settings import ConfigError, load


class PiCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")})
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main(["-C", str(self.project), "--json", *args])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_init_pi_configures_every_role_through_pi(self):
        code, payload, _ = self.invoke("init", "--yes", "--executor", "pi")
        self.assertEqual(code, 0)
        self.assertEqual(payload["executor"], "pi")
        settings = load(self.project)
        self.assertEqual(settings.get("executor.model"), "openai/gpt-6.1-sol")
        for role in ("improver", "selector", "reflector", "judge"):
            self.assertEqual(settings.role(role)["channel"], "pi")

    def test_doctor_pi_does_not_require_harness_provider_key(self):
        self.invoke("init", "--yes", "--executor", "pi")
        with patch("harness2.cli.doctor.pi_preflight", return_value="ready") as preflight:
            code, rows, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertTrue(all(row["ok"] for row in rows))
        self.assertTrue(any(row["check"] == "pi CLI" for row in rows))
        preflight.assert_called_once_with("node")

    def test_preflight_checks_pinned_runtime_node_and_credentials(self):
        host = self.root / "runtime/host.mjs"
        host.parent.mkdir()
        host.write_text("")
        manifest = host.parent / "node_modules/@earendil-works/pi-durable/package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"version":"1.4.0"}')
        binary = self.root / "node"
        binary.write_text("#!/bin/sh\necho v22.19.0\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        auth = self.root / "agent/auth.json"
        auth.parent.mkdir()
        auth.write_text('{"provider":{"token":"secret"}}')
        with (
            patch("harness2.pi_runtime.HOST", host),
            patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(auth.parent)}),
        ):
            self.assertIn("Pi Durable 1.4.0", pi_preflight(str(binary)))
            manifest.write_text('{"version":"1.3.0"}')
            with self.assertRaises(ConfigError):
                pi_preflight(str(binary))
