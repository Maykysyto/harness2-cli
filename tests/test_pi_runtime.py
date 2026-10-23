"""Pi transport keeps one durable call per stable slot."""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from harness2.pi_runtime import PiBusy, PiDescriptorMismatch, pi_session_lock, run_pi_session

FAKE_NODE = """#!/usr/bin/env python3
import json, pathlib, sys
request = pathlib.Path(sys.argv[2])
data = json.loads(request.read_text())
root = request.parent
count = root / 'calls.txt'
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
result = {'version': 1, 'slot': data['slot'], 'fingerprint': data['fingerprint'],
          'requestId': data['requestId'], 'submissionId': 7, 'status': 'done', 'answer': 'OK'}
(root / 'result.json').write_text(json.dumps(result))
(root / 'trajectory.jsonl').write_text('{"kind":"pi.assistant","model":[{"content":[{"type":"text","text":"OK"}]}]}\\n')
"""


class PiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = self.root / "fake-node"
        self.node.write_text(FAKE_NODE)
        self.node.chmod(self.node.stat().st_mode | stat.S_IXUSR)

    def call(self, **overrides):
        args = dict(
            slot="rollout/sample-1",
            session_dir=self.root / "session",
            cwd=self.root,
            model="faux/faux-1",
            effort="low",
            system="system",
            prompt="Say OK",
            context_digest="context-a",
            harness_digest="harness-a",
            node_bin=str(self.node),
            timeout=5,
        )
        return run_pi_session(**(args | overrides))

    def test_same_slot_reuses_completed_result_without_running_host_again(self):
        first = self.call()
        second = self.call()
        self.assertEqual((first.answer, second.answer), ("OK", "OK"))
        self.assertEqual((self.root / "session/calls.txt").read_text(), "1")
        descriptor = json.loads((self.root / "session/request.json").read_text())
        self.assertEqual(descriptor["requestId"], first.request_id)

    def test_changed_model_or_harness_is_rejected_before_host_run(self):
        self.call()
        with self.assertRaises(PiDescriptorMismatch):
            self.call(model="faux/another")
        with self.assertRaises(PiDescriptorMismatch):
            self.call(harness_digest="different")
        self.assertEqual((self.root / "session/calls.txt").read_text(), "1")

    def test_second_owner_cannot_enter_same_session(self):
        path = self.root / "session"
        with pi_session_lock(path):
            with self.assertRaises(PiBusy):
                self.call()
