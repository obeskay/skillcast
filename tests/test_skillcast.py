"""Tests for skillcast.

The end-to-end test runs against a synthetic screencast whose contents are
known exactly (tests/make_fixture.py), so "it extracted the right commands" is
a measurement rather than an opinion.

Stdlib only. The fixture is regenerated on demand and needs ffmpeg, tesseract
and Chrome; tests that need it skip cleanly when they are absent.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from skillcast.emit import (Skill, Step, render_claude, render_cursor,  # noqa: E402
                            slugify, validate, write)
from skillcast.extract import clean_ocr_line, looks_like_command  # noqa: E402
from skillcast.synth import build_skill, detect_stack  # noqa: E402
from skillcast.verify import verify  # noqa: E402

FIXTURE = REPO / "fixtures" / "tutorial.mp4"
TRUTH = REPO / "fixtures" / "tutorial.truth.json"

HAVE_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "tesseract"))


def sample_skill(**overrides):
    base = dict(
        name="set-up-vite",
        description="Set up a Vite project.",
        steps=[Step("Install", commands=["npm install"]),
               Step("Test", commands=["npm run test"])],
    )
    base.update(overrides)
    return Skill(**base)


class OcrRepairTest(unittest.TestCase):
    """OCR damage that would make a command unrunnable."""

    def test_double_dash_flag_survives(self):
        # tesseract renders "--" as an em dash followed by a hyphen.
        self.assertEqual(
            clean_ocr_line("$ npm create vite@latest my-app —- —-template react-ts"),
            "$ npm create vite@latest my-app -- --template react-ts")

    def test_hyphen_inside_a_word_is_not_doubled(self):
        self.assertEqual(clean_ocr_line("$ cd my—app"), "$ cd my-app")

    def test_scoped_package_name(self):
        self.assertEqual(
            clean_ocr_line("$ npm i -D @testing—library/react"),
            "$ npm i -D @testing-library/react")

    def test_space_before_extension_is_closed(self):
        self.assertEqual(clean_ocr_line("package. json"), "package.json")
        self.assertEqual(clean_ocr_line("edit vite.config. ts now"),
                         "edit vite.config.ts now")

    def test_smart_quotes_become_plain(self):
        self.assertEqual(clean_ocr_line('git commit -m “fix”'),
                         'git commit -m "fix"')

    def test_triple_hyphen_collapses(self):
        self.assertEqual(clean_ocr_line("npm run x ---flag"), "npm run x --flag")


class CommandDetectionTest(unittest.TestCase):
    def test_prompt_forms(self):
        for line in ("$ npm install", "> npm install", "# npm install",
                     "❯ npm install", "➜ npm install"):
            self.assertEqual(looks_like_command(line), "npm install", line)

    def test_bare_known_tool(self):
        self.assertEqual(looks_like_command("npm install -D vitest"),
                         "npm install -D vitest")

    def test_output_is_not_a_command(self):
        for line in ("added 148 packages in 3s", "Test Files  1 passed (1)",
                     "Done. Now run:", "up to date", "Scaffolding project in /x"):
            self.assertIsNone(looks_like_command(line), line)

    def test_prose_is_not_a_command(self):
        self.assertIsNone(looks_like_command("Now we install the dependencies"))

    def test_bare_tool_without_arguments_is_ignored(self):
        # A lone "git" on screen is a heading or a typo, not a step.
        self.assertIsNone(looks_like_command("git"))


class StackDetectionTest(unittest.TestCase):
    def test_infers_stack_from_commands(self):
        stack = detect_stack(["npm create vite@latest app", "npm i -D vitest"])
        self.assertIn("Vite", stack)
        self.assertIn("Vitest", stack)
        self.assertIn("Node.js", stack)

    def test_python_stack(self):
        self.assertIn("Python", detect_stack(["pip install pytest", "pytest -q"]))


class EmitTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Set up a Vite project!"), "set-up-a-vite-project")
        self.assertEqual(slugify("  "), "video-skill")
        self.assertEqual(slugify("A" * 200)[:64], slugify("A" * 200))

    def test_claude_frontmatter_has_required_keys(self):
        text = render_claude(sample_skill())
        self.assertTrue(text.startswith("---\n"))
        head = text.split("---")[1]
        self.assertIn("name:", head)
        self.assertIn("description:", head)

    def test_cursor_frontmatter_shape(self):
        head = render_cursor(sample_skill(globs=["**/*.ts"])).split("---")[1]
        for key in ("description:", "globs:", "alwaysApply:"):
            self.assertIn(key, head)

    def test_validate_rejects_bad_name(self):
        self.assertTrue(any("kebab-case" in p
                            for p in validate(sample_skill(name="Set Up Vite"))))

    def test_validate_rejects_empty_description(self):
        self.assertTrue(any("description" in p
                            for p in validate(sample_skill(description="  "))))

    def test_validate_rejects_skill_with_no_commands(self):
        skill = sample_skill(steps=[Step("Watch carefully")])
        self.assertTrue(any("executable" in p for p in validate(skill)))

    def test_write_produces_every_target(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            written = write(sample_skill(), tmp)
            names = {p.name for p in written}
            self.assertIn("SKILL.md", names)
            self.assertIn("AGENTS.md", names)
            self.assertIn("skill.json", names)
            self.assertTrue(any(p.suffix == ".mdc" for p in written))


class VerifyTest(unittest.TestCase):
    def codes(self, skill):
        findings, _ = verify(skill)
        return {f.code for f in findings}

    def test_clean_skill_has_no_errors(self):
        findings, _ = verify(sample_skill())
        self.assertEqual([f for f in findings if f.level == "error"], [])

    def test_destructive_command_is_an_error(self):
        skill = sample_skill(steps=[Step("Clean", commands=["rm -rf /"])])
        self.assertIn("SEC001", self.codes(skill))

    def test_curl_pipe_to_shell_is_flagged(self):
        skill = sample_skill(
            steps=[Step("Install", commands=["curl https://x.sh | sh"]),
                   Step("Run", commands=["npm test"])])
        self.assertIn("SEC001", self.codes(skill))

    def test_unbalanced_quote_is_an_error(self):
        skill = sample_skill(steps=[Step("Commit", commands=['git commit -m "oops'])])
        self.assertIn("CMD002", self.codes(skill))

    def test_leftover_em_dash_is_an_error(self):
        """A surviving em dash means the flag will not run."""
        skill = sample_skill(steps=[Step("Create", commands=["npm init —yes"])])
        self.assertIn("CMD003", self.codes(skill))

    def test_filler_text_is_an_error(self):
        skill = sample_skill(description="TODO: describe this skill")
        self.assertIn("SUB001", self.codes(skill))

    def test_tools_are_reported(self):
        _, tools = verify(sample_skill())
        self.assertIn("npm", tools)


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg/ffprobe/tesseract not installed")
class EndToEndTest(unittest.TestCase):
    """The measurement that matters: does it read the video correctly?"""

    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            subprocess.run(
                [sys.executable, str(REPO / "tests" / "make_fixture.py"),
                 str(REPO / "fixtures")],
                check=True, capture_output=True)
        cls.truth = json.loads(TRUTH.read_text())

    def test_extracts_every_command_exactly(self):
        from skillcast.extract import dedupe_commands, read_screen
        got = dedupe_commands(read_screen(FIXTURE))
        missing = [c for c in self.truth["commands"] if c not in got]
        self.assertEqual(missing, [], "commands not recovered: %s" % missing)

    def test_produces_no_phantom_commands(self):
        from skillcast.extract import dedupe_commands, read_screen
        got = dedupe_commands(read_screen(FIXTURE))
        extra = [c for c in got if c not in self.truth["commands"]]
        self.assertEqual(extra, [], "invented commands: %s" % extra)

    def test_skill_passes_its_own_verification(self):
        from skillcast.extract import read_screen
        skill = build_skill(read_screen(FIXTURE), source=FIXTURE.name)
        findings, _ = verify(skill)
        errors = [str(f) for f in findings if f.level == "error"]
        self.assertEqual(errors, [], "\n".join(errors))

    def test_every_step_of_the_tutorial_survives(self):
        from skillcast.extract import read_screen
        skill = build_skill(read_screen(FIXTURE), source=FIXTURE.name)
        self.assertEqual(len(skill.steps), self.truth["steps"])

    def test_named_file_is_recovered(self):
        from skillcast.extract import read_screen
        skill = build_skill(read_screen(FIXTURE), source=FIXTURE.name)
        for expected in self.truth["files"]:
            self.assertIn(expected, skill.files)


if __name__ == "__main__":
    unittest.main()
