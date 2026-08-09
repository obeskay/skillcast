"""Check that a generated skill is worth handing to an agent.

Generating a plausible-looking SKILL.md is easy, and that is the trap: OCR
misreads a flag, the model invents a package, and the file still looks fine.
An agent then runs it and fails in a way nobody can trace back to the video.

So every skill is checked before it ships:

  structure  the frontmatter is what each loader actually accepts
  syntax     every command parses as a shell command
  safety     nothing destructive slipped in from a misread frame
  substance  the steps say something an agent could not have guessed

None of this proves the tutorial was correct. It proves the skill is loadable,
runnable and not obviously dangerous — which is the floor, not the ceiling.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess

# Commands that should never appear in a skill derived from a tutorial. If OCR
# mangles a path, a destructive command becomes actively hostile.
DANGEROUS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(?:\s|$)"), "rm -rf on /"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"),
     "recursive force delete"),
    (re.compile(r"\bmkfs\b"), "filesystem format"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "raw write to a device"),
    (re.compile(r":\(\)\s*\{.*\};:"), "fork bomb"),
    (re.compile(r"\bchmod\s+-R\s+777\b"), "world-writable recursive chmod"),
    (re.compile(r"curl[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh"), "pipe from network to shell"),
    (re.compile(r"wget[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh"), "pipe from network to shell"),
    (re.compile(r"\bsudo\s+rm\b"), "sudo delete"),
    (re.compile(r">\s*/dev/sd[a-z]"), "overwrite of a block device"),
]

# Text that means the model padded instead of extracting.
FILLER = re.compile(
    r"\b(lorem ipsum|your[- ]?(?:command|step)[- ]?here|TODO|TBD|XXX|"
    r"insert .* here|as (?:an|a) ai|I cannot|placeholder)\b", re.I)


class Finding:
    __slots__ = ("level", "code", "message", "hint")

    def __init__(self, level, code, message, hint=""):
        self.level = level
        self.code = code
        self.message = message
        self.hint = hint

    def __repr__(self):
        return "%s %s %s" % (self.level.upper(), self.code, self.message)


def check_commands(skill):
    findings = []
    seen = set()
    for index, step in enumerate(skill.steps, 1):
        for command in step.commands:
            where = "step %d" % index
            stripped = command.strip()
            if not stripped:
                findings.append(Finding("error", "CMD001",
                                        "%s has an empty command" % where))
                continue

            # A command that will not even tokenise cannot be run.
            try:
                shlex.split(stripped)
            except ValueError as error:
                findings.append(Finding(
                    "error", "CMD002",
                    "%s: %r does not parse as a shell command (%s)"
                    % (where, stripped[:60], error),
                    "Usually an unbalanced quote introduced by OCR."))
                continue

            for pattern, label in DANGEROUS:
                if pattern.search(stripped):
                    findings.append(Finding(
                        "error", "SEC001",
                        "%s contains a destructive command (%s): %r"
                        % (where, label, stripped[:60]),
                        "A tutorial rarely calls for this. Confirm against the "
                        "video before keeping it."))
                    break

            # Leftover em dashes mean the dash repair missed a case, and the
            # flag will not be accepted by any CLI.
            if "—" in stripped or "–" in stripped:
                findings.append(Finding(
                    "error", "CMD003",
                    "%s still contains a typographic dash: %r" % (where, stripped[:60]),
                    "OCR renders '--' as an em dash. This flag will not run."))

            # No CLI accepts three consecutive hyphens. This has to be an
            # error, not a warning: it shipped once as "no problems found"
            # over a command that could never run.
            if re.search(r"-{3,}", stripped):
                findings.append(Finding(
                    "error", "CMD004",
                    "%s has a run of three or more hyphens: %r"
                    % (where, stripped[:60]),
                    "OCR disagreed with itself about how many dashes it saw. "
                    "No flag takes three."))

            key = " ".join(stripped.split())
            if key in seen:
                findings.append(Finding("warn", "CMD005",
                                        "%s repeats an earlier command: %r"
                                        % (where, stripped[:60])))
            seen.add(key)
    return findings


def check_structure(skill):
    from .emit import validate
    return [Finding("error", "STR001", problem) for problem in validate(skill)]


def check_substance(skill):
    findings = []
    text = " ".join(
        [skill.description, skill.summary]
        + [s.title + " " + s.detail + " " + s.narration + " " +
           " ".join(s.screen) for s in skill.steps])
    match = FILLER.search(text)
    if match:
        findings.append(Finding(
            "error", "SUB001",
            "the skill contains filler text (%r)" % match.group(0),
            "The model padded instead of extracting. Re-run, or the agent "
            "inherits an instruction it cannot act on."))
    if len(skill.steps) < 2:
        findings.append(Finding(
            "warn", "SUB002",
            "only %d step; a one-step skill is rarely worth loading"
            % len(skill.steps)))
    total = sum(len(s.commands) for s in skill.steps)
    if total and total < 2:
        findings.append(Finding("warn", "SUB003",
                                "only one command across the whole skill"))
    return findings


def check_guide(skill):
    if skill.kind != "guide":
        return []
    if not any(step.shortcuts or step.screen for step in skill.steps):
        return [Finding(
            "warn", "GUIDE001",
            "this guide is narration-only; review it because no shortcuts or "
            "screen evidence were captured",
            "The spoken track gives structure, but it cannot replay what was clicked.")]
    return []


def check_tools(skill, check_availability=False):
    """Optionally report which programs the skill assumes are installed."""
    findings, tools = [], set()
    for step in skill.steps:
        for command in step.commands:
            try:
                parts = shlex.split(command)
            except ValueError:
                continue
            if parts:
                tools.add(parts[0])
    if check_availability:
        for tool in sorted(tools):
            if not shutil.which(tool):
                findings.append(Finding(
                    "warn", "ENV001",
                    "the skill runs %r, which is not on this PATH" % tool,
                    "Fine if the agent runs elsewhere; worth listing as a "
                    "prerequisite either way."))
    return findings, sorted(tools)


def verify(skill, check_availability=False):
    findings = []
    findings += check_structure(skill)
    findings += check_commands(skill)
    findings += check_substance(skill)
    findings += check_guide(skill)
    tool_findings, tools = check_tools(skill, check_availability)
    findings += tool_findings
    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.level], f.code))
    return findings, tools


def shellcheck_available():
    return shutil.which("shellcheck") is not None


def shellcheck(skill):
    """Run shellcheck over the commands when it is installed."""
    if not shellcheck_available():
        return []
    script = "#!/bin/sh\n" + "\n".join(
        c for step in skill.steps for c in step.commands)
    try:
        result = subprocess.run(
            ["shellcheck", "-s", "sh", "-f", "gcc", "-"],
            input=script, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return [Finding("warn", "SHELL", "shellcheck took too long and was skipped")]
    findings = []
    for line in result.stdout.splitlines():
        match = re.match(r"^-:(\d+):\d+:\s*(\w+):\s*(.*)$", line)
        if match and match.group(2) in ("error", "warning"):
            findings.append(Finding(
                "warn" if match.group(2) == "warning" else "error",
                "SHELL", "line %s: %s" % (match.group(1), match.group(3))))
    return findings
