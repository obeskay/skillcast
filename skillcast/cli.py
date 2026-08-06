#!/usr/bin/env python3
"""skillcast — turn a screencast into a skill your coding agent can run.

    skillcast tutorial.mp4                 # write a skill into ./skill
    skillcast demo.mov -o ./out --target claude
    skillcast demo.mov --dry-run           # show what was read, write nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .emit import TARGETS, write
from .extract import (DEFAULT_SCENE_THRESHOLD, ExtractionError, dedupe_commands,
                      probe, read_screen)
from .synth import build_skill
from .verify import shellcheck, shellcheck_available, verify

SYMBOL = {"error": "x", "warn": "!", "info": "-"}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="skillcast",
        description="Turn a screencast into a skill your coding agent can run.")
    parser.add_argument("video", help="path to a screen recording")
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
    parser.add_argument("--check-tools", action="store_true",
                        help="also report commands not available on this PATH")
    parser.add_argument("--version", action="version", version="skillcast " + __version__)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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

    commands = dedupe_commands(observations)
    if not commands:
        print("skillcast: no commands were found on screen.", file=sys.stderr)
        print("  The recording may not show a terminal, or the scene threshold "
              "may be too high — try --threshold 0.005.", file=sys.stderr)
        return 1

    skill = build_skill(observations, source=video.name,
                        name=args.name, title=args.title)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
