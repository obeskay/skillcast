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


def build_skill(observations, source="", name=None, title=None):
    """Assemble a Skill from screen observations, no model involved."""
    steps, seen_commands, seen_files = [], set(), set()
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
        if not fresh and not new_files and steps:
            continue

        detail = ""
        if not fresh and new_files:
            detail = ("Edit %s. The recording shows the file on screen rather "
                      "than a command." % ", ".join("`%s`" % f for f in new_files))
        steps.append(Step(title=_title_for(observation, index),
                          detail=detail, commands=fresh))

    all_commands = [c for s in steps for c in s.commands]
    stack = detect_stack(all_commands)
    tools = sorted({c.split()[0] for c in all_commands if c.split()})

    files = sorted({
        path for observation in observations for path in observation["paths"]
        if "." in path.rsplit("/", 1)[-1] and not path.endswith("...")
    })
    urls = sorted({u for observation in observations for u in observation["urls"]})

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
