"""Turn screen observations into a skill.

Deliberately works with no API key. A tutorial that has been through scene
detection and OCR is already highly structured -- each cut is a step, the
non-command text above the commands is its title -- so the default path is
heuristic and runs offline in under a second.

An optional model pass rewrites the prose afterwards. It never invents
commands: those come from the screen, and the model is only allowed to
describe what was already extracted.
"""
from __future__ import annotations

import re

from .emit import Skill, Step, slugify
from .narrate import (align_cues, cue_text, detect_shortcuts, trim_narration)

# Frames whose text is chrome rather than content.
CHROME = re.compile(r"^(step \d+ of \d+|\d+:\d+|untitled|terminal|bash|zsh)$", re.I)

# What a project is built with, inferred from the commands themselves.
STACK_HINTS = [
    (re.compile(r"\bvite\b"), "Vite"),
    (re.compile(r"\bnext\b|create-next-app"), "Next.js"),
    (re.compile(r"\breact\b|react-ts"), "React"),
    (re.compile(r"\bvue\b"), "Vue"),
    (re.compile(r"\bsvelte\b"), "Svelte"),
    (re.compile(r"\bvitest\b"), "Vitest"),
    (re.compile(r"\bjest\b"), "Jest"),
    (re.compile(r"\bpytest\b"), "pytest"),
    (re.compile(r"\bdocker\b"), "Docker"),
    (re.compile(r"\bkubectl\b|\bhelm\b"), "Kubernetes"),
    (re.compile(r"\bterraform\b"), "Terraform"),
    (re.compile(r"\bcargo\b"), "Rust"),
    (re.compile(r"\bgo (?:run|build|test|mod)\b"), "Go"),
    (re.compile(r"\bpip\b|\bpython3?\b|\buv\b|\bpoetry\b"), "Python"),
    (re.compile(r"\bnpm\b|\bpnpm\b|\byarn\b|\bbun\b"), "Node.js"),
    (re.compile(r"\bgit\b"), "Git"),
]

GLOBS_FOR_STACK = {
    "Node.js": ["**/package.json", "**/*.ts", "**/*.tsx", "**/*.js"],
    "React": ["**/*.tsx", "**/*.jsx"],
    "Python": ["**/*.py", "**/pyproject.toml", "**/requirements.txt"],
    "Rust": ["**/*.rs", "**/Cargo.toml"],
    "Go": ["**/*.go"],
    "Docker": ["**/Dockerfile", "**/docker-compose*.y*ml"],
    "Terraform": ["**/*.tf"],
}


def _title_for(observation, index):
    """The most title-like line on the frame."""
    for line in observation["lines"]:
        text = line.strip()
        if not text or CHROME.match(text):
            continue
        if text.startswith(("$", ">", "#")):
            continue
        # Trailing step counters bleed in from the corner of the frame.
        text = re.sub(r"\s*step \d+ of \d+\s*$", "", text, flags=re.I).strip()
        if 3 <= len(text) <= 90:
            return text
    if observation["commands"]:
        first = observation["commands"][0].split()
        return "Run %s" % " ".join(first[:3])
    return "Step %d" % index


def detect_stack(commands):
    joined = " ".join(commands).lower()
    found = []
    for pattern, label in STACK_HINTS:
        if pattern.search(joined) and label not in found:
            found.append(label)
    return found


def infer_prerequisites(stack, tools):
    prerequisites = []
    if "Node.js" in stack:
        prerequisites.append("Node.js and a package manager (npm, pnpm or yarn)")
    if "Python" in stack:
        prerequisites.append("Python 3 and pip")
    if "Docker" in stack:
        prerequisites.append("Docker running locally")
    if "Rust" in stack:
        prerequisites.append("Rust and cargo")
    if "Go" in stack:
        prerequisites.append("Go")
    for tool in ("git", "gh", "kubectl", "terraform"):
        if tool in tools and not any(tool in p.lower() for p in prerequisites):
            prerequisites.append("`%s` on PATH" % tool)
    return prerequisites


def _observation_time(observation):
    for key in ("at_seconds", "timestamp_s", "time_s", "timestamp"):
        if observation.get(key) is not None:
            return float(observation[key])
    return 0.0


def _screen_lines(observation):
    commands = set(observation.get("commands") or [])
    lines = []
    for line in observation.get("lines") or []:
        line = str(line).strip()
        if line and line not in commands and line not in lines:
            lines.append(line)
    return lines


def _observed_files(observations):
    return sorted({
        path for observation in observations for path in observation.get("paths", [])
        if "." in path.rsplit("/", 1)[-1] and not path.endswith("...")
    })


def _observed_urls(observations):
    return sorted({u for observation in observations for u in observation.get("urls", [])})


def build_skill(observations, source="", name=None, title=None, narration=None,
                narration_language=""):
    """Assemble a Skill from screen observations, no model involved."""
    steps, seen_commands, seen_files = [], set(), set()
    aligned = align_cues(narration or [], observations) if narration else [[] for _ in observations]
    for index, observation in enumerate(observations, 1):
        fresh = []
        for command in observation["commands"]:
            key = " ".join(command.split())
            if key not in seen_commands:
                seen_commands.add(key)
                fresh.append(command)

        # "Now edit package.json" is a real step with no command in it, so a
        # commandless frame is only dropped when it also shows nothing new.
        new_files = [
            path for path in observation["paths"]
            if path not in seen_files and "." in path.rsplit("/", 1)[-1]
            and not path.endswith("...")
        ]
        seen_files.update(new_files)
        note = cue_text(aligned[index - 1]) if aligned[index - 1] else ""
        if not fresh and not new_files and not note and steps:
            continue

        detail = ""
        if not fresh and new_files:
            detail = ("Edit %s. The recording shows the file on screen rather "
                      "than a command." % ", ".join("`%s`" % f for f in new_files))
        steps.append(Step(title=_title_for(observation, index),
                          detail=detail, commands=fresh))
        steps[-1].at_seconds = _observation_time(observation)
        steps[-1].narration = note

    all_commands = [c for s in steps for c in s.commands]
    stack = detect_stack(all_commands)
    tools = sorted({c.split()[0] for c in all_commands if c.split()})

    files = _observed_files(observations)
    urls = _observed_urls(observations)

    subject = title or (steps[0].title if steps else "this workflow")
    display = subject.strip().rstrip(".")
    skill_name = name or slugify(display)

    stack_phrase = ", ".join(stack) if stack else "the tools shown"
    description = (
        "%s. Use when the task involves %s: this skill carries the exact "
        "commands demonstrated in the source recording, in order."
        % (display, stack_phrase)
    )

    globs = []
    for entry in stack:
        for glob in GLOBS_FOR_STACK.get(entry, []):
            if glob not in globs:
                globs.append(glob)

    summary = (
        "%d steps taken from a screen recording. The commands were read off "
        "the screen rather than transcribed from narration, so they are the "
        "literal text that was run." % len(steps)
    )

    return Skill(
        name=skill_name,
        description=description[:1024],
        summary=summary,
        prerequisites=infer_prerequisites(stack, tools),
        steps=steps,
        files=files,
        urls=urls,
        globs=globs,
        source=source,
        narration_language=narration_language,
    )


def _chapter_windows(chapters, duration_s, cues):
    records = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        try:
            start = float(chapter.get("start_time", chapter.get("start", 0)) or 0)
        except (TypeError, ValueError):
            continue
        end_value = chapter.get("end_time", chapter.get("end"))
        try:
            end = float(end_value) if end_value is not None else None
        except (TypeError, ValueError):
            end = None
        records.append((start, end, str(chapter.get("title") or "").strip()))
    # "<Untitled Chapter 1>" is YouTube's placeholder, not a title — and the
    # VTT tag stripper eats angle brackets, so it must go before any use.
    records = [(start, end,
                "" if re.match(r"^<Untitled Chapter \d+>$", title) else title)
               for start, end, title in records]
    records.sort(key=lambda item: item[0])
    windows = []
    fallback_end = max(
        [float(duration_s or 0)]
        + [cue.end_s for cue in cues]
        + [0.0])
    for index, (start, end, chapter_title) in enumerate(records):
        if end is None:
            end = records[index + 1][0] if index + 1 < len(records) else fallback_end
        if end <= start:
            end = float("inf") if index == len(records) - 1 else records[index + 1][0]
        windows.append((start, end, chapter_title))
    return windows


def _narration_windows(cues):
    """Make roughly 75-second guide sections, cutting at a real silence."""
    cues = sorted(cues, key=lambda cue: (cue.start_s, cue.end_s))
    if not cues:
        return []
    windows = []
    current = [cues[0]]
    window_start = cues[0].start_s
    for cue in cues[1:]:
        gap = cue.start_s - current[-1].end_s
        elapsed = cue.start_s - window_start
        if elapsed >= 60 and gap > 2:
            windows.append((window_start, current[-1].end_s, current))
            current = [cue]
            window_start = cue.start_s
        elif elapsed >= 90:
            windows.append((window_start, current[-1].end_s, current))
            current = [cue]
            window_start = cue.start_s
        else:
            current.append(cue)
    windows.append((window_start, current[-1].end_s, current))
    return windows


def _guide_title(chapter_title, cues, index):
    candidates = [chapter_title] + [cue.text for cue in list(cues)[:3]]
    for candidate in candidates:
        title = trim_narration(candidate or "", max_sentences=1, max_chars=60)
        if title:
            return title
    return "Guide step %d" % index


# Words that appear on the chrome of almost any GUI (Blender, Photoshop,
# Figma, Excel, an IDE). A line of OCR soup almost never contains one whole.
_UI_VOCAB = frozenset(
    "file edit view window help layout scene render preferences save open new "
    "select tools properties settings layer node material texture camera light "
    "output input transform rotate scale duplicate delete undo redo play pause "
    "frame timeline console terminal editor panel menu button search filter "
    "import export project library assets browser preview object mode mesh "
    "modifier vertex edge face curve animation keyframe workspace addons "
    "toolbar sidebar canvas zoom snap grid align".split())


def _plausible_ui_line(line):
    """Keep text that could be a menu, button or panel label; drop OCR noise.

    A GUI frame at tutorial resolution yields mostly glyph soup, so evidence
    must earn its place: a real UI word, whole, four letters or more. What
    survives is evidence of what was visible — not a claim of exact text.
    """
    if len(line) < 3 or len(line) > 80:
        return False
    letters = sum(char.isalpha() for char in line)
    if letters < 3 or letters / float(len(line)) < 0.6:
        return False
    if not re.search(r"[A-Za-z]{3,}", line):
        return False
    words = re.findall(r"[A-Za-z]+", line)
    if words and sum(1 for word in words if len(word) <= 2) / float(len(words)) >= 0.5:
        return False  # scattered one-and-two-letter tokens are glyph soup
    if re.search(r"(.)\1{3,}", line):  # aaaaa runs are misread textures
        return False
    if re.search(r"[^\w\s]{3,}", line):  # symbol soup like <=.-@
        return False
    return any(word.lower() in _UI_VOCAB and len(word) >= 4 for word in words)


def _evidence_for_window(observations, start_s, end_s):
    evidence = []
    for observation in observations:
        at_seconds = _observation_time(observation)
        if at_seconds < start_s or at_seconds >= end_s:
            continue
        for line in _screen_lines(observation):
            if _plausible_ui_line(line) and line not in evidence:
                evidence.append(line)
    return evidence[:8]


def build_guide_skill(observations, narration, source="", name=None, title=None,
                      chapters=None, duration_s=None, narration_language=""):
    """Build a non-executable guide from chapters or the spoken track."""
    cues = sorted(narration or [], key=lambda cue: (cue.start_s, cue.end_s))
    windows = _chapter_windows(chapters, duration_s, cues) if chapters else []
    if not windows:
        windows = [(start, end, "") for start, end, _ in _narration_windows(cues)]

    steps = []
    for index, (start_s, end_s, chapter_title) in enumerate(windows, 1):
        window_cues = [
            cue for cue in cues
            if cue.end_s > start_s and cue.start_s < end_s
        ]
        screen = _evidence_for_window(observations, start_s, end_s)
        narration_text = cue_text(window_cues)
        shortcuts = detect_shortcuts(" ".join(
            [narration_text] + screen))
        steps.append(Step(
            title=_guide_title(chapter_title, window_cues, index),
            at_seconds=start_s,
            narration=narration_text,
            screen=screen,
            shortcuts=shortcuts,
        ))

    # The topic the narration builds toward: the first chapter that has a real
    # title, else the opening sentence, else nothing — and nothing stays honest.
    first_topic = ""
    for start, end, chapter_title in windows:
        if chapter_title:
            first_topic = _guide_title(chapter_title, [], 1)
            break
    if not first_topic:
        first_topic = trim_narration(cues[0].text if cues else "",
                                     max_sentences=1, max_chars=60)
    display = (title or first_topic or "tutorial guide").strip().rstrip(".")
    skill_name = name or slugify(display, fallback="tutorial-guide")
    if first_topic:
        description = "Follow the narration to %s." % first_topic
    else:
        description = "A step-by-step guide built from the video's narration."
    summary = (
        "%d guide steps taken from narration and what was visible on screen. "
        "It records where to look and what the tutorial teaches; it does not "
        "invent executable commands." % len(steps)
    )
    return Skill(
        name=skill_name,
        description=description[:1024],
        summary=summary,
        steps=steps,
        # A GUI tutorial does not touch files in any meaningful way — paths
        # lifted off a Blender or Photoshop frame are OCR noise, so guides
        # carry none. URLs survive: a real link on screen matches a strict
        # pattern that noise almost never does.
        files=[],
        urls=_observed_urls(observations),
        source=source,
        kind="guide",
        narration_language=narration_language,
    )


PROSE_PROMPT = """You are given the literal on-screen text extracted from a
developer screencast, plus the commands that were run.

Rewrite ONLY the prose: a title, a one-sentence description of when an agent
should use this skill, and a short detail line for each step.

Hard rules:
- Do NOT invent, correct or add commands. They were read off the screen.
- Do NOT reorder the steps.
- If you cannot tell what a step does, say so plainly rather than guessing.
- Reply with JSON: {"title": str, "description": str, "steps": [{"detail": str}]}
  with exactly one entry per step, in order.

Steps:
%s
"""


def prose_prompt(skill):
    """The prompt for the optional model pass. Exposed so it can be inspected."""
    blocks = []
    for index, step in enumerate(skill.steps, 1):
        commands = "\n".join("    " + c for c in step.commands) or "    (no commands)"
        blocks.append("%d. %s\n%s" % (index, step.title, commands))
    return PROSE_PROMPT % "\n".join(blocks)


def apply_prose(skill, data):
    """Merge a model's prose back in, keeping every extracted command intact."""
    if not isinstance(data, dict):
        return skill
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    if description:
        skill.description = description[:1024]
    if title:
        skill.name = slugify(title, fallback=skill.name)
    for step, incoming in zip(skill.steps, data.get("steps") or []):
        if isinstance(incoming, dict):
            detail = (incoming.get("detail") or "").strip()
            if detail:
                step.detail = detail
    return skill
