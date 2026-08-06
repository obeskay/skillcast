#!/usr/bin/env python3
"""skillcast — turn a screencast into a skill your coding agent can run.

    skillcast tutorial.mp4                 # write a skill into ./skill
    skillcast demo.mov -o ./out --target claude
    skillcast demo.mov --dry-run           # show what was read, write nothing
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .emit import TARGETS, write
from .fetch import FetchError, download, looks_like_url
from .extract import (DEFAULT_SCENE_THRESHOLD, ExtractionError, dedupe_commands,
                      known_tool_commands, probe, read_screen,
                      screen_confidence)
from .synth import build_skill
from .verify import shellcheck, shellcheck_available, verify

SYMBOL = {"error": "x", "warn": "!", "info": "-"}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="skillcast",
        description="Turn a screencast into a skill your coding agent can run.")
    parser.add_argument("video",
                        help="path to a screen recording, or a video URL "
                             "(YouTube, Vimeo, Loom — anything yt-dlp handles)")
    parser.add_argument("-o", "--out", default="skill",
                        help="output directory (default: ./skill)")
    parser.add_argument("--target", action="append", choices=sorted(TARGETS),
                        help="output format; repeatable (default: all)")
    parser.add_argument("--name", help="skill name (default: derived from the video)")
    parser.add_argument("--title", help="override the detected title")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCENE_THRESHOLD,
                        help="scene-change sensitivity; lower finds more frames "
                             "(default: %(default)s, tuned for screencasts)")
    parser.add_argument("--max-frames", type=int, default=120,
                        help="cap on frames analysed (default: %(default)s)")
    parser.add_argument("--lang", default="eng", help="tesseract language (default: eng)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what was read without writing files")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="reuse a browser's session when a site blocks "
                             "anonymous downloads (chrome, firefox, safari, edge)")
    parser.add_argument("--check-tools", action="store_true",
                        help="also report commands not available on this PATH")
    parser.add_argument("--version", action="version", version="skillcast " + __version__)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    # Pasting a link is the obvious thing to try, so accept it.
    downloaded_to = None
    source_label = None
    if looks_like_url(args.video):
        try:
            video, info = download(
                args.video,
                cookies_from_browser=args.cookies_from_browser,
                on_progress=lambda m: print("skillcast: %s" % m))
        except FetchError as error:
            print("skillcast: %s" % error, file=sys.stderr)
            return 2
        downloaded_to = video.parent
        source_label = info.get("title") or args.video
        print("skillcast: got %s (%.1f MB)"
              % (video.name, video.stat().st_size / 1e6))
    else:
        video = Path(args.video)
        if not video.exists():
            print("skillcast: %s does not exist" % video, file=sys.stderr)
            return 2

    try:
        info = probe(video)
        observations = read_screen(
            video, threshold=args.threshold,
            max_frames=args.max_frames, lang=args.lang)
    except ExtractionError as error:
        print("skillcast: %s" % error, file=sys.stderr)
        return 2

    # Footage with no terminal in it still yields OCR noise that can pass for a
    # prompt. Refuse it rather than emitting a skill made of stray characters.
    confidence = screen_confidence(observations)
    commands = dedupe_commands(observations)
    recognised = known_tool_commands(observations)
    if commands and not recognised:
        print("skillcast: this does not look like a screen recording.",
              file=sys.stderr)
        print("  %d candidate command(s) were found but not one uses a program "
              "I recognise, which is what OCR noise looks like — not anything "
              "that was typed." % len(commands), file=sys.stderr)
        print("  skillcast reads terminals and editors. A recording of hands, "
              "slides or a talking head has nothing for it to lift.",
              file=sys.stderr)
        return 1

    if not commands:
        print("skillcast: no commands were found on screen.", file=sys.stderr)
        print("  The recording may not show a terminal, or the scene threshold "
              "may be too high — try --threshold 0.005.", file=sys.stderr)
        return 1

    skill = build_skill(observations, source=source_label or video.name,
                        name=args.name, title=args.title or source_label)
    findings, tools = verify(skill, check_availability=args.check_tools)
    findings += shellcheck(skill)

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warn"]
    failed = bool(errors) or (args.strict and bool(warnings))

    if args.json:
        payload = {
            "video": str(video),
            "duration_s": round(info["duration_s"], 2),
            "frames": len(observations),
            "screen_confidence": round(confidence, 3),
            "skill": json.loads(skill.to_json()),
            "tools": tools,
            "findings": [
                {"level": f.level, "code": f.code, "message": f.message, "hint": f.hint}
                for f in findings
            ],
            "ok": not failed,
        }
        if not args.dry_run and not failed:
            payload["written"] = [str(p) for p in write(
                skill, args.out, args.target or tuple(sorted(TARGETS)))]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1 if failed else 0

    print("read %s — %.0fs, %d scene changes, %d commands"
          % (video.name, info["duration_s"], len(observations), len(commands)))
    if len(recognised) < len(commands):
        print("  note: %d of %d commands use a program I do not recognise — "
              "check those closely" % (len(commands) - len(recognised), len(commands)))
    print("skill: %s (%d steps)" % (skill.name, len(skill.steps)))
    for index, step in enumerate(skill.steps, 1):
        print("  %d. %s" % (index, step.title))
        for command in step.commands:
            print("       $ %s" % command)

    if findings:
        print()
        for finding in findings:
            print("  %s %s  %s" % (SYMBOL[finding.level], finding.code, finding.message))
            if finding.hint:
                print("      %s" % finding.hint)
    if not shellcheck_available():
        print("\n  (install shellcheck for deeper command checking)")

    if failed:
        print("\nnot written: fix the errors above, or re-run with a different "
              "--threshold", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    written = write(skill, args.out, args.target or tuple(sorted(TARGETS)))
    print()
    for path in written:
        print("  wrote %s" % path)
    print("\nRead the commands before running them. OCR is good, not perfect.")
    if downloaded_to:
        shutil.rmtree(downloaded_to, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
