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
from .narrate import fetch_subtitles, load_sidecar
from .replay import ReplayError, replay
from .route import RouteError, run_route
from .synth import build_guide_skill, build_skill
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
    narration = parser.add_mutually_exclusive_group()
    narration.add_argument("--narration", dest="narration", action="store_true",
                           help="use a subtitle track when available")
    narration.add_argument("--no-narration", dest="narration", action="store_false",
                           help="do not fetch or read subtitles")
    parser.set_defaults(narration=None)
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="reuse a browser's session when a site blocks "
                             "anonymous downloads (chrome, firefox, safari, edge)")
    parser.add_argument("--check-tools", action="store_true",
                        help="also report commands not available on this PATH")
    parser.add_argument("--version", action="version", version="skillcast " + __version__)
    return parser


def build_route_parser():
    parser = argparse.ArgumentParser(
        prog="skillcast route",
        description="Turn a playlist into an ordered learning route.")
    parser.add_argument("playlist", help="playlist URL")
    parser.add_argument("goal", nargs="?", help="what you want to learn")
    parser.add_argument("-o", "--out", default="route",
                        help="output directory (default: ./route)")
    parser.add_argument("--sort", choices=("playlist", "general-first"),
                        default="playlist", help="curriculum order")
    parser.add_argument("--limit", type=int, help="process at most N videos")
    narration = parser.add_mutually_exclusive_group()
    narration.add_argument("--narration", dest="narration", action="store_true",
                           help="fetch subtitles and allow guide skills")
    narration.add_argument("--no-narration", dest="narration", action="store_false",
                           help="do not fetch subtitles")
    parser.set_defaults(narration=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="list the route without downloading videos or writing files")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="reuse a browser session for private or blocked playlists")
    return parser


def route_main(argv):
    args = build_route_parser().parse_args(argv)
    try:
        result = run_route(
            args.playlist, output=args.out, goal=args.goal or "",
            sort_mode=args.sort, limit=args.limit,
            narration_enabled=args.narration is not False,
            dry_run=args.dry_run,
            cookies_from_browser=args.cookies_from_browser,
        )
    except RouteError as error:
        print("skillcast: %s" % error, file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "playlist": result["playlist"],
            "goal": result["goal"],
            "path": result["path"],
            "reordered": result["reordered"],
            "videos": [
                {key: video.get(key) for key in
                 ("title", "duration_s", "ability", "skill_path", "kind")}
                for video in result["videos"]
            ],
            "failures": result["failures"],
            "ok": not result["failures"],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not result["failures"] else 1
    if args.sort == "general-first":
        print("route: general-first order: %s" %
              " -> ".join(entry.get("title", "Untitled video")
                          for entry in result["entries"]))
    if args.dry_run:
        print("dry run — %d video(s), nothing downloaded or written" %
              len(result["entries"]))
        return 0
    print("route: %d video(s) written to %s" %
          (len(result["videos"]), result["path"]))
    for failure in result["failures"]:
        print("  ! %s: %s" % (failure["title"], failure["reason"]))
    return 0 if not result["failures"] else 1


def build_replay_parser():
    from .replay import (DEFAULT_CHECKPOINT_MS, DEFAULT_GRACE_MS,
                         DEFAULT_PACE_MS)
    parser = argparse.ArgumentParser(
        prog="skillcast replay",
        description="Turn a guide skill into a Peekaboo replay script that "
                    "presses the keys the narrator pressed, paced as recorded.")
    parser.add_argument("source",
                        help="a skill.json, or the directory skillcast wrote")
    parser.add_argument("-o", "--out",
                        help="script path (default: ./<skill-name>.peekaboo.json)")
    parser.add_argument("--pace-ms", type=int, default=DEFAULT_PACE_MS,
                        help="pause after each keyed step (default: %(default)s)")
    parser.add_argument("--checkpoint-ms", type=int, default=DEFAULT_CHECKPOINT_MS,
                        help="pause at keyless steps, which are manual "
                             "checkpoints (default: %(default)s)")
    parser.add_argument("--grace-ms", type=int, default=DEFAULT_GRACE_MS,
                        help="lead-in so you can focus the target app "
                             "(default: %(default)s)")
    parser.add_argument("--run", action="store_true",
                        help="execute now via peekaboo — sends real keystrokes "
                             "to the frontmost app")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def replay_main(argv):
    args = build_replay_parser().parse_args(argv)
    try:
        result = replay(args.source, out=args.out, pace_ms=args.pace_ms,
                        checkpoint_ms=args.checkpoint_ms, grace_ms=args.grace_ms,
                        execute=args.run)
    except ReplayError as error:
        print("skillcast: %s" % error, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        report = result.get("report") or {}
        return 0 if report.get("success", True) else 1
    print("replay pack: %s" % result["script"])
    print("  %d keystroke(s), %d manual checkpoint(s) — from %s"
          % (result["hotkeys"], result["checkpoints"], result["skill"]))
    if args.run:
        report = result.get("report") or {}
        data = report.get("data") or {}
        print("  peekaboo: %d/%d steps ok"
              % (data.get("completedSteps", 0), data.get("totalSteps", 0)))
        if not report.get("success", bool(data)):
            print("  the pack did not finish — check peekaboo's permissions",
                  file=sys.stderr)
            return 1
    else:
        print("\nFocus the app the tutorial uses, then:")
        print("  peekaboo run %s --no-remote" % result["script"])
        print("Steps without keys are pauses for you to click along — the "
              "script paces, you click.")
    return 0


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "route":
        return route_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "replay":
        return replay_main(raw_argv[1:])
    args = build_parser().parse_args(raw_argv)

    # Pasting a link is the obvious thing to try, so accept it.
    downloaded_to = None
    source_label = None
    source_info = {}
    sidecar = None
    if looks_like_url(args.video):
        try:
            video, source_info = download(
                args.video,
                cookies_from_browser=args.cookies_from_browser,
                on_progress=None if args.json else lambda m: print("skillcast: %s" % m))
        except FetchError as error:
            print("skillcast: %s" % error, file=sys.stderr)
            return 2
        downloaded_to = video.parent
        source_label = source_info.get("title") or args.video
        if not args.json:
            print("skillcast: got %s (%.1f MB)"
                  % (video.name, video.stat().st_size / 1e6))
        if args.narration is not False:
            track = fetch_subtitles(
                args.video, video.parent,
                cookies_from_browser=args.cookies_from_browser)
        else:
            track = None
    else:
        video = Path(args.video)
        if not video.exists():
            print("skillcast: %s does not exist" % video, file=sys.stderr)
            return 2
        sidecar = load_sidecar(video)
        use_narration = args.narration if args.narration is not None else bool(sidecar)
        track = sidecar if use_narration else None

    try:
        info = probe(video)
        info.update({key: value for key, value in source_info.items()
                     if key not in info or key in ("duration_s", "chapters", "title")})
        observations = read_screen(
            video, threshold=args.threshold,
            max_frames=args.max_frames, lang=args.lang)
    except ExtractionError as error:
        print("skillcast: %s" % error, file=sys.stderr)
        return 2

    # Real commands beat narration; narration beats OCR noise. The refusal
    # below only fires when neither is present, which is what OCR noise with
    # nothing to say looks like.
    confidence = screen_confidence(observations)
    commands = dedupe_commands(observations)
    recognised = known_tool_commands(observations)
    if recognised:
        skill = build_skill(
            observations, source=source_label or video.name,
            name=args.name, title=args.title or source_label,
            narration=track.cues if track else None,
            narration_language=track.language if track else "",
        )
    elif track and track.cues:
        skill = build_guide_skill(
            observations, track.cues, source=source_label or video.name,
            name=args.name, title=args.title or source_label,
            chapters=source_info.get("chapters") or [],
            duration_s=info.get("duration_s"),
            narration_language=track.language,
        )
    elif commands:
        print("skillcast: this does not look like a screen recording.",
              file=sys.stderr)
        print("  %d candidate command(s) were found but not one uses a program "
              "I recognise, which is what OCR noise looks like — not anything "
              "that was typed." % len(commands), file=sys.stderr)
        print("  skillcast reads terminals and editors. A recording of hands, "
              "slides or a talking head has nothing for it to lift.",
              file=sys.stderr)
        return 1
    else:
        print("skillcast: no commands were found on screen.", file=sys.stderr)
        print("  The recording may not show a terminal, or the scene threshold "
              "may be too high — try --threshold 0.005.", file=sys.stderr)
        return 1
    findings, tools = verify(skill, check_availability=args.check_tools)
    if skill.kind != "guide":
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
    print("skill: %s (%d steps, %s)" % (skill.name, len(skill.steps), skill.kind))
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
