"""Pull the executable truth out of a screencast.

A dev tutorial says two different things at once. The narration carries intent
-- "now we install the test runner" -- while the screen carries the part you
can actually run: `npm install -D vitest`. Transcripts alone lose the second
one, which is exactly the part an agent needs.

So this reads the screen. Scene detection finds the moments where the screen
changed, OCR lifts the text off those frames, and a set of deliberately narrow
heuristics decide what is a command, a path, or a URL.

Only ffmpeg and tesseract are required; both are shelled out to, so there are
no Python dependencies.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


FRAME_TIMEOUT_S = 300
OCR_TIMEOUT_S = 45
PROBE_TIMEOUT_S = 60

# Prompt characters that commonly precede a shell command on screen.
PROMPT = re.compile(r"^\s*(?:\$|>|#|❯|➜|PS\s*[^>]*>)\s+(?P<cmd>\S.*)$")

# Programs whose presence makes a bare line very likely to be a command even
# without a prompt, because tutorials often crop the prompt out.
KNOWN_TOOLS = (
    "npm", "npx", "pnpm", "yarn", "bun", "deno", "node",
    "pip", "pip3", "python", "python3", "uv", "poetry", "pytest",
    "git", "gh", "docker", "kubectl", "helm", "terraform",
    "cargo", "go", "make", "brew", "apt", "apt-get", "curl", "wget",
    "mkdir", "cd", "cp", "mv", "rm", "ls", "cat", "chmod", "export",
    "vite", "next", "tsc", "eslint", "prettier", "vitest", "jest",
)

# Lines that look like output rather than input.
OUTPUT_NOISE = re.compile(
    r"^\s*(added|removed|changed|found|audited|Done|Success|✓|✔|×|✗|error|warn|"
    r"info|Test Files|Tests|Duration|Start at|PASS|FAIL|\d+ packages?|"
    r"up to date|npm notice|Scaffolding)",
    re.I,
)

PATH_LIKE = re.compile(r"[\w.-]+/[\w./-]+|\b[\w-]+\.(?:json|ts|tsx|js|jsx|py|toml|ya?ml|md|lock|cfg|ini|env)\b")
URL_LIKE = re.compile(r"https?://[^\s'\"<>)\]]+")


class ExtractionError(RuntimeError):
    pass


def _require(tool):
    path = shutil.which(tool)
    if not path:
        raise ExtractionError(
            "%s is required but was not found on PATH. "
            "Install it and try again." % tool
        )
    return path


def probe(video):
    """Duration and frame size, so callers can sanity-check the input."""
    _require("ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=width,height",
             "-of", "json", str(video)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError("ffprobe took too long to read %s" % video)
    if result.returncode != 0:
        raise ExtractionError("cannot read %s: %s" % (video, result.stderr.strip()))
    data = json.loads(result.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    return {
        "duration_s": float(data.get("format", {}).get("duration", 0) or 0),
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


# Screencasts need a far lower scene threshold than ordinary video. When a
# terminal advances, only the glyphs change: the dark background dominates the
# frame and the difference score stays tiny. Measured on a terminal recording,
# real cuts score 0.017-0.024, so the usual 0.3-0.4 default finds nothing at
# all. This is the single setting most likely to make a tool like this look
# broken on exactly the footage it is meant for.
DEFAULT_SCENE_THRESHOLD = 0.01


def scene_frames(video, out_dir, threshold=DEFAULT_SCENE_THRESHOLD,
                 max_frames=120, every=None):
    """Extract one frame per visual change.

    A tutorial holds still while the presenter talks, then cuts. Sampling on a
    fixed interval either floods you with duplicates or walks straight past a
    command that was only on screen briefly. Scene detection follows the edits
    instead.
    """
    _require("ffmpeg")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if every:
        select = "fps=1/%g" % every
    else:
        select = "select='gt(scene,%g)+eq(n,0)'" % threshold

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "info", "-i", str(video),
             "-vf", select + ",showinfo", "-vsync", "vfr",
             "-frames:v", str(max_frames), "-q:v", "2",
             str(out_dir / "frame_%04d.png")],
            capture_output=True, text=True, timeout=FRAME_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError(
            "ffmpeg took too long to extract frames; reduce --max-frames and try again")
    frames = sorted(out_dir.glob("frame_*.png"))
    if not frames:
        raise ExtractionError(
            "no frames extracted from %s. The file may not be a video, or the "
            "scene threshold may be too high." % video
        )
    timestamps = []
    for line in (result.stderr or "").splitlines():
        match = re.search(r"showinfo.*?\bn:\s*\d+.*?pts_time:\s*([0-9.]+)", line)
        if match:
            timestamps.append(float(match.group(1)))
    if len(timestamps) < len(frames):
        # The frame order is still useful if an older ffmpeg build did not
        # print showinfo; the normal path above keeps real video timestamps.
        timestamps.extend([float(index) for index in range(len(timestamps), len(frames))])
    (out_dir / "timestamps.json").write_text(
        json.dumps(timestamps[:len(frames)]), encoding="utf-8")
    return frames


# How much video may pass between captured frames before the detection is
# assumed to have missed steps.
SPARSE_SECONDS = 12.0


def adaptive_frames(video, out_dir, threshold=DEFAULT_SCENE_THRESHOLD,
                    max_frames=120):
    """Find scene changes, lowering the bar when the footage is subtle.

    A single command appearing in a screen already full of code moves a tiny
    fraction of the pixels. Measured on a dense IDE recording, those cuts score
    0.0001-0.002 -- another order of magnitude below a bare terminal, and far
    under any sane default. Fixing that by lowering the global default would
    flood ordinary recordings with compression noise instead.

    So the threshold adapts: try, and if the result is sparse for the video's
    length, try again ten times more sensitive. If detection still finds almost
    nothing, fall back to sampling on a fixed interval, which is worse but never
    silently returns one frame for a ten-minute tutorial.
    """
    duration = probe(video).get("duration_s") or 0
    attempts = []
    current = threshold
    for _ in range(3):
        work = Path(out_dir) / ("t%s" % str(current).replace(".", "_"))
        frames = scene_frames(video, work, threshold=current, max_frames=max_frames)
        attempts.append((current, frames))
        # Enough coverage for the running time? Then stop.
        if not duration or len(frames) >= duration / SPARSE_SECONDS:
            return frames, {"mode": "scene", "threshold": current}
        current /= 10.0

    # Detection stayed sparse. Sample on an interval instead.
    threshold_used, frames = max(attempts, key=lambda a: len(a[1]))
    if duration and len(frames) < duration / SPARSE_SECONDS:
        every = max(2.0, duration / min(max_frames, 40))
        sampled = scene_frames(video, Path(out_dir) / "interval",
                               max_frames=max_frames, every=every)
        if len(sampled) > len(frames):
            return sampled, {"mode": "interval", "every_s": round(every, 1)}
    return frames, {"mode": "scene", "threshold": threshold_used}


def ocr(image, lang="eng", psm=6):
    """Read text off one frame.

    tesseract's `-` stdout mode is unreliable across builds, so this always
    writes to a temp file. Dark terminal themes need no preprocessing: tesseract
    handles light-on-dark directly, and inverting first measurably changes
    nothing.
    """
    _require("tesseract")
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "out"
        try:
            result = subprocess.run(
                ["tesseract", str(image), str(stem), "--psm", str(psm), "-l", lang],
                capture_output=True, text=True, timeout=OCR_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise ExtractionError(
                "tesseract took too long to read %s; reduce --max-frames and try again"
                % image)
        produced = stem.with_suffix(".txt")
        if not produced.exists():
            raise ExtractionError(
                "tesseract produced no output for %s: %s"
                % (image, result.stderr.strip()[:200])
            )
        return produced.read_text(encoding="utf-8", errors="replace")


# A program name: lowercase-ish word, or an explicit path. Anything that does
# not start with one of these is not a command, whatever preceded it on screen.
PROGRAM = re.compile(r"^(?:[A-Za-z_][\w.+-]{1,39}|\.{0,2}/[\w./+-]+)$")

# Prompt-looking noise is everywhere in OCR of non-terminal footage. Requiring a
# plausible program name is what stops "$ 'a" and "> B" becoming steps.
MIN_COMMAND_LENGTH = 3


def plausible_command(command):
    """Could a shell actually run this?

    Added after a hand-held phone video produced "'a" and "B" as commands, and
    the verifier reported no problems. OCR of anything that is not a terminal
    generates prompt-shaped noise constantly, so the program name is the gate.
    """
    command = command.strip()
    parts = command.split()
    if not parts or not PROGRAM.match(parts[0]):
        return False
    # "ls" and "cd" are shorter than the floor but perfectly real.
    if len(command) < MIN_COMMAND_LENGTH and parts[0] not in KNOWN_TOOLS:
        return False
    # A bare program with no arguments is only credible if we know the program.
    if len(parts) == 1 and parts[0] not in KNOWN_TOOLS:
        return False
    return True


def repair_command(command):
    """Final repair pass applied only to text already judged to be a command.

    Dash runs are collapsed here rather than in clean_ocr_line because a line of
    output may legitimately be a "-------" separator, while no CLI flag has ever
    taken three hyphens. Different tesseract builds disagree about how many
    dashes they saw, so "-- --template" comes back as "----" as often as "---".
    """
    return re.sub(r"-{3,}", "--", command)


def looks_like_command(line):
    """Is this line something a human typed, rather than something printed?"""
    line = line.strip()
    if not line or len(line) > 400:
        return None
    prompt = PROMPT.match(line)
    if prompt:
        candidate = prompt.group("cmd").strip()
        if not candidate or OUTPUT_NOISE.match(candidate):
            return None
        repaired = repair_command(candidate)
        return repaired if plausible_command(repaired) else None
    if OUTPUT_NOISE.match(line):
        return None
    first = line.split()[0] if line.split() else ""
    if first in KNOWN_TOOLS and len(line.split()) > 1:
        repaired = repair_command(line)
        return repaired if plausible_command(repaired) else None
    return None


def clean_ocr_line(line):
    """Undo the substitutions OCR reliably makes on monospaced terminal text.

    Dashes are the damaging case, and they are ambiguous: tesseract renders a
    lone hyphen and a double hyphen with the same em dash glyph. Measured
    against a terminal recording the pattern is consistent:

        "npm ... my-app -- --template"  reads as  "my-app \u2014- \u2014-template"
        "cd my-app"                     reads as  "cd my\u2014app"

    So an em dash followed by a hyphen is "--", an em dash between two word
    characters is "-", and a leading one introduces a long flag. Get this wrong
    and every long flag in the output is unrunnable.
    """
    for old, new in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'),
                     ("\u201d", '"'), ("\u00a0", " ")):
        line = line.replace(old, new)
    line = re.sub("\u2014-", "--", line)                 # "\u2014-template" -> "--template"
    line = re.sub(r"(?<=\w)\u2014(?=\w)", "-", line)      # "my\u2014app"      -> "my-app"
    line = line.replace("\u2014", "--")
    line = line.replace("\u2013", "-")
    # No CLI flag uses three hyphens; a run that long is a substitution artefact.
    line = re.sub(r"-{3,}(?=[A-Za-z])", "--", line)
    # OCR frequently inserts a space before a file extension, turning
    # "package.json" into "package. json" and hiding it from path detection.
    line = re.sub(
        r"(\w)\.\s+(json|jsonc|ts|tsx|js|jsx|mjs|cjs|py|rb|go|rs|toml|ya?ml|md|"
        r"lock|txt|cfg|ini|env|sh|sql|html|css)\b",
        r"\1.\2", line, flags=re.I)
    return line.rstrip()


def read_screen(video, work_dir=None, threshold=DEFAULT_SCENE_THRESHOLD,
                max_frames=120, lang="eng"):
    """Full screen pass: frames -> OCR -> structured observations."""
    owned = work_dir is None
    work = Path(work_dir or tempfile.mkdtemp(prefix="skillcast-"))
    try:
        frames, strategy = adaptive_frames(video, work / "frames", threshold, max_frames)
        timestamps = []
        if frames:
            timestamp_file = Path(frames[0]).parent / "timestamps.json"
            if timestamp_file.exists():
                try:
                    timestamps = json.loads(timestamp_file.read_text(encoding="utf-8"))
                except (TypeError, ValueError, OSError):
                    timestamps = []
        observations = []
        for index, frame in enumerate(frames):
            text = ocr(frame, lang=lang)
            lines = [clean_ocr_line(l) for l in text.splitlines()]
            lines = [l for l in lines if l.strip()]
            commands, paths, urls = [], [], []
            for line in lines:
                found = looks_like_command(line)
                if found:
                    commands.append(found)
                paths.extend(PATH_LIKE.findall(line))
                urls.extend(URL_LIKE.findall(line))
            observations.append({
                "strategy": strategy,
                "frame": index,
                "file": str(frame),
                "at_seconds": float(timestamps[index]) if index < len(timestamps) else float(index),
                "lines": lines,
                "commands": commands,
                "paths": sorted(set(paths)),
                "urls": sorted(set(urls)),
            })
        return observations
    finally:
        if owned:
            shutil.rmtree(work, ignore_errors=True)


# Fragments of text that indicate a technical screen: paths, flags, code
# punctuation, known tools. Footage of hands or faces produces none of it.
TECHNICAL_HINT = re.compile(
    r"(^|\s)(?:--?[a-z][\w-]*|[\w.-]+/[\w./-]+|https?://|[\w-]+\.(?:json|ts|tsx|js|py|toml|ya?ml|md)"
    r"|\{|\}|=>|;$|\(\)|import |const |def |function )", re.I)


def known_tool_commands(observations):
    """Commands whose program is one we actually recognise.

    This is the sharpest signal that the footage contains a terminal. A phone
    video of someone's hands produces prompt-shaped noise whose "program" is a
    stray letter; a real screencast produces npm, git, docker, pip. Measured on
    a genuine terminal recording every command clears this bar, and on the hand
    video none do.
    """
    hits = []
    for observation in observations:
        for command in observation["commands"]:
            parts = command.split()
            if parts and parts[0] in KNOWN_TOOLS:
                hits.append(command)
    return hits


def screen_confidence(observations):
    """How much of what was read looks like a technical screen at all.

    Returns 0..1. Used only as a soft warning: titles and program output push
    this down even on a real screencast (a genuine terminal fixture scores about
    0.18), so it is too blunt to reject on. known_tool_commands does the
    rejecting.
    """
    total = technical = 0
    for observation in observations:
        for line in observation["lines"]:
            stripped = line.strip()
            if len(stripped) < 3:
                continue
            total += 1
            if TECHNICAL_HINT.search(stripped) or any(
                    stripped.split()[0] == tool for tool in KNOWN_TOOLS):
                technical += 1
    return (technical / total) if total else 0.0


def dedupe_commands(observations):
    """Commands in first-seen order, without the repeats scene cuts produce."""
    seen, ordered = set(), []
    for observation in observations:
        for command in observation["commands"]:
            key = " ".join(command.split())
            if key not in seen:
                seen.add(key)
                ordered.append(command)
    return ordered
