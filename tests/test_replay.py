import json
import tempfile
import unittest
from pathlib import Path

from skillcast.replay import (ReplayError, build_script, load_skill, replay,
                              shortcut_to_hotkey)


GUIDE = {
    "name": "model-a-flower",
    "steps": [
        {"title": "Intro", "shortcuts": [], "commands": []},
        {"title": "Base of the flower", "shortcuts": ["Ctrl+R", "S"], "commands": []},
        {"title": "Stem", "shortcuts": ["X"], "commands": []},
    ],
}


class ShortcutMappingTest(unittest.TestCase):
    def test_modifier_combo(self):
        self.assertEqual(shortcut_to_hotkey("Ctrl+R"),
                         {"key": "r", "modifiers": ["ctrl"]})
        self.assertEqual(shortcut_to_hotkey("Cmd+Shift+P"),
                         {"key": "p", "modifiers": ["command", "shift"]})

    def test_single_key(self):
        self.assertEqual(shortcut_to_hotkey("S"), {"key": "s", "modifiers": []})

    def test_doubtful_keys_are_refused(self):
        with self.assertRaises(ReplayError):
            shortcut_to_hotkey("Meta+X")
        with self.assertRaises(ReplayError):
            shortcut_to_hotkey("Ctrl+SomethingLong")


class ScriptAssemblyTest(unittest.TestCase):
    def test_order_and_step_kinds(self):
        script, hotkeys, checkpoints = build_script(GUIDE)
        steps = script["steps"]
        self.assertEqual(hotkeys, 3)
        self.assertEqual(checkpoints, 1)
        self.assertEqual(steps[0]["stepId"], "settle-in")
        self.assertEqual(steps[1]["stepId"], "checkpoint-1")
        self.assertEqual(steps[2]["command"], "hotkey")
        self.assertEqual(steps[2]["params"]["hotkey"]["_0"],
                         {"key": "r", "modifiers": ["ctrl"]})
        self.assertEqual(steps[3]["params"]["hotkey"]["_0"],
                         {"key": "s", "modifiers": []})
        self.assertEqual(steps[4]["stepId"], "pace-2")
        self.assertEqual(steps[5]["params"]["hotkey"]["_0"],
                         {"key": "x", "modifiers": []})
        self.assertEqual(steps[6]["stepId"], "pace-3")

    def test_pacing_is_configurable(self):
        script, _, _ = build_script(GUIDE, pace_ms=7, checkpoint_ms=9, grace_ms=5)
        sleeps = {s["stepId"]: s["params"]["sleep"]["_0"]["duration"]
                  for s in script["steps"] if s["command"] == "sleep"}
        self.assertEqual(sleeps["settle-in"], 5)
        self.assertEqual(sleeps["pace-2"], 7)
        self.assertEqual(sleeps["checkpoint-1"], 9)


class LoadSkillTest(unittest.TestCase):
    def test_directory_and_file_both_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            path.write_text(json.dumps(GUIDE), encoding="utf-8")
            from_dir, _ = load_skill(tmp)
            from_file, _ = load_skill(str(path))
            self.assertEqual(from_dir["name"], "model-a-flower")
            self.assertEqual(from_file["name"], "model-a-flower")

    def test_garbage_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ReplayError):
                load_skill(tmp)  # no skill.json inside
            bad = Path(tmp) / "skill.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ReplayError):
                load_skill(str(bad))
            bad.write_text("{}", encoding="utf-8")
            with self.assertRaises(ReplayError):
                load_skill(str(bad))


class ReplayEndToEndTest(unittest.TestCase):
    def test_writes_executable_shaped_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "skill.json"
            skill_path.write_text(json.dumps(GUIDE), encoding="utf-8")
            out = Path(tmp) / "out.peekaboo.json"
            result = replay(str(skill_path), out=str(out))
            script = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(result["hotkeys"], 3)
            self.assertIn("model-a-flower", script["description"])
            for step in script["steps"]:
                self.assertIn("stepId", step)
                self.assertIn("command", step)
                # The enum-wrapped shape installed Peekaboo builds decode.
                self.assertEqual(len(step["params"]), 1)
                case = next(iter(step["params"].values()))
                self.assertIn("_0", case)

    def test_empty_guide_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_path = Path(tmp) / "skill.json"
            skill_path.write_text(json.dumps({"name": "empty", "steps": []}),
                                  encoding="utf-8")
            with self.assertRaises(ReplayError):
                replay(str(skill_path), out=str(Path(tmp) / "o.json"))


if __name__ == "__main__":
    unittest.main()
