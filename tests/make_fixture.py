#!/usr/bin/env python3
"""Render a synthetic screencast whose contents are known exactly.

Any claim that skillcast "extracted the right commands" needs a video where the
right answer is not a matter of opinion. This builds one: a scripted terminal
tutorial, plus the ground truth as JSON beside it.

Frames are rendered by headless Chrome rather than ffmpeg's drawtext, because
ffmpeg is frequently built without libfreetype, and because Chrome produces the
same antialiased text a real screen recording would -- which is what the OCR
stage has to survive.

    python3 tests/make_fixture.py fixtures/

Requires ffmpeg and Google Chrome. No Python dependencies.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT = 1280, 720
SECONDS_PER_STEP = 3
FPS = 8

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

# The script of the fake tutorial. `screen` is what a viewer sees; `commands`
# is what an agent must come away able to run.
STEPS = [
    {
        "title": "Set up a Vite project",
        "screen": ["$ npm create vite@latest my-app -- --template react-ts",
                   "",
                   "Scaffolding project in /home/dev/my-app...",
                   "Done. Now run:"],
        "commands": ["npm create vite@latest my-app -- --template react-ts"],
    },
    {
        "title": "Install dependencies",
        "screen": ["$ cd my-app", "$ npm install", "", "added 148 packages in 3s"],
        "commands": ["cd my-app", "npm install"],
    },
    {
        "title": "Add Vitest",
        "screen": ["$ npm install -D vitest @testing-library/react",
                   "", "added 42 packages in 2s"],
        "commands": ["npm install -D vitest @testing-library/react"],
    },
    {
        "title": "Configure the test script",
        "screen": ["package.json", "", '  "scripts": {', '    "dev": "vite",',
                   '    "test": "vitest run"', "  }"],
        "commands": [],
        "files": ["package.json"],
    },
    {
        "title": "Run the tests",
        "screen": ["$ npm run test", "", "Test Files  1 passed (1)",
                   "     Tests  3 passed (3)"],
        "commands": ["npm run test"],
    },
]


def find_chrome():
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


def escape_html(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def frame_html(step, index):
    lines = []
    for line in step["screen"]:
        if not line:
            lines.append('<div class="row">&nbsp;</div>')
            continue
        cls = "cmd" if line.startswith("$") else "out"
        lines.append('<div class="row %s">%s</div>' % (cls, escape_html(line)))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:{WIDTH}px; height:{HEIGHT}px; background:#0d1117; overflow:hidden; }}
  .bar {{ height:54px; background:#161b22; display:flex; align-items:center;
          justify-content:space-between; padding:0 28px;
          font:600 21px -apple-system,'Helvetica Neue',sans-serif; color:#8b949e; }}
  .step {{ font-size:17px; color:#484f58; font-weight:500; }}
  .term {{ padding:34px 40px; }}
  .row {{ font:30px/1.55 Menlo,Monaco,'Courier New',monospace; color:#c9d1d9;
          white-space:pre; }}
  .cmd {{ color:#58a6ff; }}
</style></head><body>
  <div class="bar"><span>{escape_html(step['title'])}</span>
  <span class="step">step {index + 1} of {len(STEPS)}</span></div>
  <div class="term">{''.join(lines)}</div>
</body></html>"""


def main(argv=None):
    argv = argv or sys.argv[1:]
    out_dir = Path(argv[0] if argv else "fixtures")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        print("make_fixture: ffmpeg is required", file=sys.stderr)
        return 2
    chrome = find_chrome()
    if not chrome:
        print("make_fixture: Chrome or Chromium is required", file=sys.stderr)
        return 2

    work = out_dir / "_frames"
    work.mkdir(exist_ok=True)
    shots = []
    for index, step in enumerate(STEPS):
        page = work / f"step{index}.html"
        page.write_text(frame_html(step, index), encoding="utf-8")
        shot = work / f"step{index}.png"
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
             f"--screenshot={shot}", f"--window-size={WIDTH},{HEIGHT}", str(page)],
            check=True, capture_output=True,
        )
        if not shot.exists():
            print("make_fixture: Chrome produced no screenshot", file=sys.stderr)
            return 1
        shots.append(shot)

    # Hold each still for a few seconds, the way a real tutorial pauses.
    listing = work / "concat.txt"
    listing.write_text("".join(
        "file '%s'\nduration %d\n" % (s.resolve(), SECONDS_PER_STEP) for s in shots
    ) + "file '%s'\n" % shots[-1].resolve())

    video = out_dir / "tutorial.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-vf", f"fps={FPS},format=yuv420p", str(video)],
        check=True,
    )
    shutil.rmtree(work)

    truth = {
        "video": video.name,
        "duration_s": len(STEPS) * SECONDS_PER_STEP,
        "steps": len(STEPS),
        "commands": [c for s in STEPS for c in s["commands"]],
        "files": [f for s in STEPS for f in s.get("files", [])],
        "titles": [s["title"] for s in STEPS],
    }
    (out_dir / "tutorial.truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    print("wrote %s (%ds, %d steps, %d commands)"
          % (video, truth["duration_s"], truth["steps"], len(truth["commands"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
