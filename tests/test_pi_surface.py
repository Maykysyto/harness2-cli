"""Pi harness surface exposes the files the Durable host actually reads."""

import tempfile
import unittest
from pathlib import Path

from harness2.substrate.pi import PiSubstrate


class PiSurfaceTests(unittest.TestCase):
    def test_import_export_keeps_instructions_and_skills_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("Instructions")
            skill = root / ".agents/skills/checker/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\ndescription: Check\n---\n")
            (root / "secret.txt").write_text("private")
            surface = PiSubstrate()
            tree = surface.import_tree(root)
            self.assertEqual(set(tree), {"AGENTS.md", ".agents/skills/checker/SKILL.md"})
            output = root / "export"
            surface.export_tree(tree, output, base={})
            self.assertEqual((output / "AGENTS.md").read_text(), "Instructions")
            self.assertFalse((output / "secret.txt").exists())

    def test_trajectory_contains_agent_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "trajectory.jsonl"
            stream.write_text(
                '{"kind":"pi.tool-result","model":[{"role":"toolResult","content":[{"type":"text","text":"proof"}]}]}\n'
            )
            rendered = PiSubstrate().read_trajectory(stream, obs_trunc=500, max_chars=1000)
            self.assertIn("proof", rendered)
            self.assertIn("OBSERVATION", rendered)
