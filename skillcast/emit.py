"""Write the extracted knowledge out in the formats agents actually load.

Each agent reads a different file in a different place, but they all want the
same thing underneath: a short description of when this applies, followed by
concrete steps. So the pipeline produces one intermediate `Skill` and renders
it per target, rather than prompting a model separately for each.

Formats verified against real installed skills and current vendor docs:
  Claude Code  .claude/skills/<name>/SKILL.md   frontmatter: name, description
  Cursor       .cursor/rules/<name>.mdc         frontmatter: description, globs, alwaysApply
  Codex/others AGENTS.md                        plain markdown, no frontmatter
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Claude's loader rejects a name that is not lowercase kebab-case, and truncates
# a description past 1024 characters.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024


@dataclass
class Step:
    title: str
    detail: str = ""
    commands: list = field(default_factory=list)
    at_seconds: float = 0.0
    narration: str = ""
    screen: list = field(default_factory=list)
    shortcuts: list = field(default_factory=list)


@dataclass
class Skill:
    name: str
    description: str
    summary: str = ""
    prerequisites: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    files: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    globs: list = field(default_factory=list)
    source: str = ""
    kind: str = "runbook"
    narration_language: str = ""

    def to_json(self):
        return json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n"


def slugify(text, fallback="video-skill"):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:MAX_NAME].strip("-")
    return slug or fallback


def validate(skill):
    """Problems that would make a real agent refuse or mis-load the skill."""
    problems = []
    if not skill.name:
        problems.append("name is empty")
    elif not NAME_RE.match(skill.name):
        problems.append(
            "name %r must be lowercase kebab-case; Claude's loader rejects "
            "capitals, spaces and underscores" % skill.name)
    elif len(skill.name) > MAX_NAME:
        problems.append("name is %d characters; the limit is %d"
                        % (len(skill.name), MAX_NAME))

    if not skill.description.strip():
        problems.append("description is empty; without it the agent never "
                        "knows when to invoke the skill")
    elif len(skill.description) > MAX_DESCRIPTION:
        problems.append("description is %d characters; the limit is %d"
                        % (len(skill.description), MAX_DESCRIPTION))

    if not skill.steps:
        problems.append("no steps; the skill would tell the agent nothing")
    if skill.kind != "guide" and not any(step.commands for step in skill.steps):
        problems.append("no commands in any step; nothing here is executable")
    if skill.kind == "guide" and not any(
            step.narration or step.screen for step in skill.steps):
        problems.append("no narration or screen evidence; the guide would tell the agent nothing")
    return problems


def _timestamp(seconds):
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        total = 0
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return "%02d:%02d:%02d" % (hours, minutes, seconds) if hours else "%02d:%02d" % (minutes, seconds)


def _body(skill):
    """The shared markdown body, used by every target."""
    out = []
    if skill.summary:
        out.append(skill.summary.strip() + "\n")
    if skill.prerequisites:
        out.append("## Prerequisites\n")
        out.extend("- %s" % p for p in skill.prerequisites)
        out.append("")
    out.append("## Steps\n")
    for index, step in enumerate(skill.steps, 1):
        out.append("### %d. %s\n" % (index, step.title))
        if step.detail:
            out.append(step.detail.strip() + "\n")
        if step.commands:
            out.append("```bash")
            out.extend(step.commands)
            out.append("```\n")
        if step.narration:
            quoted = step.narration.replace('"', '\\"').replace("\n", " ").strip()
            out.append('> "%s" — %s\n' % (quoted, _timestamp(step.at_seconds)))
        if step.screen:
            out.append("**Seen on screen**\n")
            out.extend("- `%s`" % str(line).replace("`", "") for line in step.screen)
            out.append("")
        if step.shortcuts:
            out.append("**Shortcuts**\n")
            out.extend("- `%s`" % shortcut for shortcut in step.shortcuts)
            out.append("")
    if skill.files:
        out.append("## Files touched\n")
        out.extend("- `%s`" % f for f in skill.files)
        out.append("")
    if skill.urls:
        out.append("## References\n")
        out.extend("- %s" % u for u in skill.urls)
        out.append("")
    if skill.narration_language:
        out.append("Narration track: `%s`." % skill.narration_language)
    if skill.source:
        out.append("---\n")
        if skill.kind == "guide":
            out.append("Built from `%s` by skillcast. The guide records what was "
                       "said and what was visible; it does not replay clicks." % skill.source)
        else:
            out.append("Extracted from `%s` by skillcast. The commands above were "
                       "read off the screen rather than transcribed from narration; "
                       "read them before running them." % skill.source)
    return "\n".join(out).rstrip() + "\n"


def render_claude(skill):
    description = skill.description.replace("\n", " ").strip()
    return ("---\nname: %s\ndescription: %s\nkind: %s\n---\n\n# %s\n\n%s"
            % (skill.name, json.dumps(description), skill.kind, skill.name.replace("-", " ").title(),
               _body(skill)))


def render_cursor(skill):
    globs = ", ".join(skill.globs) if skill.globs else ""
    description = skill.description.replace("\n", " ").strip()
    return ("---\ndescription: %s\nglobs: %s\nalwaysApply: false\nkind: %s\n---\n\n# %s\n\n%s"
            % (description, globs, skill.kind, skill.name.replace("-", " ").title(), _body(skill)))


def render_agents_md(skill):
    return ("# %s\n\n%s\n%s"
            % (skill.name.replace("-", " ").title(),
               skill.description.strip(), "\n" + _body(skill)))


TARGETS = {
    "claude": (lambda s: Path(".claude/skills") / s.name / "SKILL.md", render_claude),
    "cursor": (lambda s: Path(".cursor/rules") / ("%s.mdc" % s.name), render_cursor),
    "agents": (lambda s: Path("AGENTS.md"), render_agents_md),
}


def write(skill, out_dir, targets=("claude", "cursor", "agents")):
    """Render the skill for each target under out_dir. Returns written paths."""
    out_dir = Path(out_dir)
    written = []
    for target in targets:
        if target not in TARGETS:
            raise ValueError("unknown target %r; expected one of %s"
                             % (target, ", ".join(sorted(TARGETS))))
        relative, render = TARGETS[target]
        destination = out_dir / relative(skill)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render(skill), encoding="utf-8")
        written.append(destination)
    manifest = out_dir / "skill.json"
    manifest.write_text(skill.to_json(), encoding="utf-8")
    written.append(manifest)
    return written
