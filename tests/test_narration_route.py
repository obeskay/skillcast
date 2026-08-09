import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skillcast.emit import render_claude
from skillcast.narrate import (Cue, align_cues_to_scenes, dedupe_rolling,
                               detect_shortcuts, load_sidecar,
                               parse_srt, parse_vtt)
from skillcast.route import assemble_route, general_first, parse_flat_playlist
from skillcast.synth import build_guide_skill, _plausible_ui_line
from skillcast.verify import verify


REPO = Path(__file__).resolve().parents[1]


class LiveCertRegressionTest(unittest.TestCase):
    """Bugs found by the first real-YouTube certification run (2026-08-09)."""

    def test_rolling_auto_captions_do_not_repeat(self):
        cues = parse_vtt("""WEBVTT

00:00:00.000 --> 00:00:02.000
Hi, here's a quick tutorial about how to

00:00:02.000 --> 00:00:04.000
Hi, here's a quick tutorial about how to
make a low poly flower in Blender. I'm

00:00:04.000 --> 00:00:06.000
make a low poly flower in Blender. I'm
just going to add a loop cut
""")
        joined = " ".join(cue.text for cue in cues)
        self.assertEqual(
            joined,
            "Hi, here's a quick tutorial about how to "
            "make a low poly flower in Blender. I'm just going to add a loop cut")

    def test_dedupe_rolling_keeps_manual_subs_untouched(self):
        cues = [Cue(0, 2, "one two"), Cue(2, 4, "three four")]
        self.assertEqual([cue.text for cue in dedupe_rolling(cues)],
                         ["one two", "three four"])

    def test_spoken_shortcuts_are_detected(self):
        found = detect_shortcuts(
            "by pressing Ctrl and R. And then press S. Now pressing G to grab.")
        self.assertIn("Ctrl+R", found)
        self.assertIn("S", found)
        self.assertIn("G", found)
        self.assertNotIn("T", detect_shortcuts("pressing the three key"))

    def test_ocr_noise_is_not_screen_evidence(self):
        self.assertFalse(_plausible_ui_line("« Ce oe OO hh OOO R BS"))
        self.assertFalse(_plausible_ui_line("wa <=. -@ eo"))
        self.assertFalse(_plausible_ui_line("z"))
        self.assertFalse(_plausible_ui_line("Seen eaaaaaa.aS.a.a.a.aaQ0QS"))
        self.assertTrue(_plausible_ui_line("Object Mode"))
        self.assertTrue(_plausible_ui_line("Array Modifier"))

    def test_guide_title_and_description_never_ship_empty(self):
        skill = build_guide_skill(
            [], [Cue(0, 5, ""), Cue(5, 10, "Model the petals with the array modifier.")],
            chapters=[{"start_time": 0, "end_time": 5, "title": ""},
                      {"start_time": 5, "end_time": 10, "title": "Petals"}],
            duration_s=10)
        self.assertNotEqual(skill.steps[0].title.strip(), "")
        self.assertTrue(skill.description.endswith("."))
        self.assertNotIn("to .", skill.description)

    def test_youtubes_untitled_chapter_placeholder_is_not_a_title(self):
        skill = build_guide_skill(
            [], [Cue(0, 5, "Start with a cube and scale it down.")],
            chapters=[{"start_time": 0, "end_time": 149,
                       "title": "<Untitled Chapter 1>"},
                      {"start_time": 149, "end_time": 328,
                       "title": "Proportional Editing"}],
            duration_s=328)
        self.assertNotIn("Untitled", skill.steps[0].title)
        self.assertNotIn("Guide step", skill.description)
        self.assertIn("Proportional Editing", skill.description)


class NarrationParserTest(unittest.TestCase):
    def test_vtt_timestamps_tags_and_multiline_cues(self):
        cues = parse_vtt("""WEBVTT

1
00:00:01.500 --> 00:00:03.750 position:10% align:start
<v Speaker><i>First line</i>
second &amp; line

00:04.000 --> 00:00:05.000
Press <c.green>G</c>.
""")
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_s, 1.5)
        self.assertEqual(cues[0].end_s, 3.75)
        self.assertEqual(cues[0].text, "First line second & line")
        self.assertEqual(cues[1].text, "Press G.")

    def test_srt_timestamp_comma_and_multiline(self):
        cues = parse_srt("""1
00:00:00,000 --> 00:00:02,250
One line
Two lines
""")
        self.assertEqual([(cue.start_s, cue.end_s, cue.text) for cue in cues],
                         [(0.0, 2.25, "One line Two lines")])

    def test_cues_align_by_half_open_overlap(self):
        cues = [Cue(1, 4, "one"), Cue(3.5, 8, "two")]
        aligned = align_cues_to_scenes(cues, [0, 4, 7])
        self.assertEqual([["one", "two"], ["two"], ["two"]],
                         [[cue.text for cue in group] for group in aligned])

    def test_local_sidecar_is_loaded_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "lesson.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".vtt").write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n",
                encoding="utf-8")
            track = load_sidecar(video)
        self.assertEqual(track.language, "unknown")
        self.assertEqual(track.cues[0].text, "Hello")


class GuideSynthesisTest(unittest.TestCase):
    def setUp(self):
        self.cues = parse_vtt("""WEBVTT

00:00:01.000 --> 00:00:04.000
We will shape the first petal. Press G to move it.

00:00:11.000 --> 00:00:14.000
Now save the flower with Ctrl+S.
""")
        self.observations = [
            {"at_seconds": 2, "lines": ["Blender", "Object Mode", "Menu"],
             "commands": [], "paths": [], "urls": []},
            {"at_seconds": 12, "lines": ["Blender", "Petal", "Ctrl+S"],
             "commands": [], "paths": [], "urls": []},
        ]

    def test_guide_uses_narration_and_screen_without_inventing_commands(self):
        skill = build_guide_skill(
            self.observations, self.cues, source="flower.mp4", title="Model a flower",
            chapters=[{"start_time": 0, "end_time": 10, "title": "Shape the petal"},
                      {"start_time": 10, "end_time": 20, "title": "Save the flower"}],
            narration_language="en")
        self.assertEqual(skill.kind, "guide")
        self.assertEqual([step.commands for step in skill.steps], [[], []])
        self.assertIn("Object Mode", skill.steps[0].screen)
        self.assertIn("G", skill.steps[0].shortcuts)
        self.assertIn("Ctrl+S", skill.steps[1].shortcuts)
        findings, _ = verify(skill)
        self.assertEqual([finding for finding in findings if finding.level == "error"], [])
        self.assertNotIn("GUIDE001", {finding.code for finding in findings})
        rendered = render_claude(skill)
        self.assertIn("kind: guide", rendered)
        self.assertIn('> "We will shape the first petal. Press G to move it." — 00:00', rendered)
        self.assertIn("Seen on screen", rendered)
        payload = json.loads(skill.to_json())
        self.assertEqual(payload["kind"], "guide")
        self.assertEqual(payload["steps"][0]["narration"],
                         "We will shape the first petal. Press G to move it.")


class RefusalTest(unittest.TestCase):
    def test_no_commands_and_no_narration_keeps_refusal(self):
        from skillcast import cli
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            handle.write(b"local placeholder")
            handle.flush()
            output = io.StringIO()
            errors = io.StringIO()
            with mock.patch.object(cli, "probe", return_value={"duration_s": 1}), \
                    mock.patch.object(cli, "read_screen", return_value=[
                        {"lines": ["A talking head"], "commands": [], "paths": [], "urls": []}
                    ]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main([handle.name, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("no commands", errors.getvalue())


class RouteTest(unittest.TestCase):
    def test_general_first_is_stable_and_deterministic(self):
        entries = [
            {"title": "Advanced lighting"},
            {"title": "Introduction"},
            {"title": "Basics"},
            {"title": "Advanced deep dive"},
        ]
        ordered = general_first(entries)
        self.assertEqual([entry["title"] for entry in ordered], [
            "Introduction", "Basics", "Advanced lighting", "Advanced deep dive"
        ])
        self.assertEqual(ordered, general_first(entries))

    def test_flat_playlist_fixture_and_route_assembly(self):
        playlist = parse_flat_playlist(REPO / "fixtures" / "flat-playlist.json")
        self.assertEqual(playlist["title"], "Blender Flower Route")
        self.assertEqual(playlist["entries"][0]["url"],
                         "https://www.youtube.com/watch?v=flower-advanced")
        markdown = assemble_route(
            playlist["title"], "learn Blender from the basics", [
                {"title": "Blender Introduction", "duration_s": 300,
                 "ability": "Blender interface overview",
                 "skill_path": "01-blender-introduction/.claude/skills/blender-introduction/SKILL.md"},
                {"title": "Flower Modeling Basics", "duration_s": 360,
                 "ability": "Model the first petals",
                 "skill_path": "02-flower-modeling-basics/.claude/skills/flower-modeling-basics/SKILL.md"},
            ],
            failures=[{"title": "Advanced lighting", "reason": "no commands or narration were found"}],
        )
        self.assertIn("Goal: learn Blender from the basics", markdown)
        self.assertIn("cumulative 11m 00s", markdown)
        self.assertIn("01-blender-introduction/.claude/skills", markdown)
        self.assertIn("Videos not included", markdown)
        self.assertIn("Learn this route:", markdown)
        self.assertNotIn("OCR", markdown)
        self.assertNotIn("schema", markdown)
        self.assertNotIn("slug", markdown)


if __name__ == "__main__":
    unittest.main()
