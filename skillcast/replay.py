"""Turn a guide skill into a Peekaboo replay script.

A guide knows which keys the narrator pressed, and at what moment. It does not
know where anything sits on your screen — no recording matches your windows.
So a replay pack presses exactly those keys, paces the steps the way the video
paced them, and leaves the clicking to you. That is the honest slice of replay.

The emitted file targets the enum-wrapped ``_0`` script shape because the
Peekaboo builds in the wild (3.0.0) decode it, and newer builds keep it as a
legacy alias. Both shapes were probed live against `peekaboo run`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# What detect_shortcuts can produce, mapped to Peekaboo's modifier names.
MODIFIER_NAMES = {"ctrl": "ctrl", "cmd": "command", "shift": "shift", "alt": "alt"}

# Multi-character keys Peekaboo accepts; anything longer than one character
# that is not in this list was misread and must not become a keystroke.
NAMED_KEYS = {"space", "return", "tab", "escape", "delete", "backspace",
              "up", "down", "left", "right", "home", "end", "pageup", "pagedown"}

DEFAULT_PACE_MS = 4000
DEFAULT_CHECKPOINT_MS = 12000
DEFAULT_GRACE_MS = 3000


class ReplayError(RuntimeError):
    pass


def load_skill(path):
    """Read a skill.json, accepting the file itself or its directory."""
    candidate = Path(path)
    if candidate.is_dir():
        if (candidate / "skill.json").exists():
            candidate = candidate / "skill.json"
        else:
            raise ReplayError(
                "no skill.json in %s — point replay at the directory skillcast wrote"
                % path)
    if not candidate.exists():
        raise ReplayError("%s does not exist" % candidate)
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReplayError("%s is not readable JSON" % candidate)
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ReplayError(
            "%s is not a skill.json — it has no steps" % candidate)
    return data, candidate


def shortcut_to_hotkey(shortcut):
    """Map 'Ctrl+R' to Peekaboo's key/modifiers, refusing anything doubtful."""
    parts = [part.strip() for part in str(shortcut).split("+")]
    key = parts[-1].lower()
    if len(key) > 1 and key not in NAMED_KEYS:
        raise ReplayError(
            "%r is not a key I can press — refusing to guess" % shortcut)
    if not key:
        raise ReplayError("empty key in shortcut %r" % shortcut)
    modifiers = []
    for modifier in parts[:-1]:
        name = MODIFIER_NAMES.get(modifier.lower())
        if not name:
            raise ReplayError(
                "%r uses a modifier I do not know" % shortcut)
        if name not in modifiers:
            modifiers.append(name)
    return {"key": key, "modifiers": modifiers}


def build_script(skill, pace_ms=DEFAULT_PACE_MS,
                 checkpoint_ms=DEFAULT_CHECKPOINT_MS, grace_ms=DEFAULT_GRACE_MS):
    """Assemble the .peekaboo.json document. Deterministic, no side effects."""
    steps = [{
        "stepId": "settle-in",
        "command": "sleep",
        "params": {"sleep": {"_0": {"duration": grace_ms}}},
    }]
    hotkey_count = 0
    checkpoint_count = 0
    for index, step in enumerate(skill["steps"], 1):
        if not isinstance(step, dict):
            continue
        shortcuts = step.get("shortcuts") or []
        if shortcuts:
            for shortcut in shortcuts:
                hotkey = shortcut_to_hotkey(shortcut)
                steps.append({
                    "stepId": "step-%d-%s" % (index, hotkey["key"]),
                    "command": "hotkey",
                    "params": {"hotkey": {"_0": hotkey}},
                })
                hotkey_count += 1
            steps.append({
                "stepId": "pace-%d" % index,
                "command": "sleep",
                "params": {"sleep": {"_0": {"duration": pace_ms}}},
            })
        else:
            # A step with no keys is a manual checkpoint: the script waits
            # while you do the clicking the narrator showed.
            steps.append({
                "stepId": "checkpoint-%d" % index,
                "command": "sleep",
                "params": {"sleep": {"_0": {"duration": checkpoint_ms}}},
            })
            checkpoint_count += 1
    script = {
        "description": (
            "Replay pack for %s — the keys the narrator pressed, paced as "
            "recorded. Steps without keys are pauses for you to click along."
            % (skill.get("name") or "the tutorial")),
        "steps": steps,
    }
    return script, hotkey_count, checkpoint_count


def write_script(script, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    return out


def run_script(path):
    """Execute the pack through peekaboo. This sends real keystrokes."""
    if not shutil.which("peekaboo"):
        raise ReplayError(
            "peekaboo is not installed — brew install steipede/tap/peekaboo")
    try:
        result = subprocess.run(
            ["peekaboo", "run", str(path), "--json", "--no-remote"],
            capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        raise ReplayError("peekaboo took over an hour — the pack is stuck")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise ReplayError("peekaboo did not return a report: %s"
                          % (result.stderr.strip() or result.stdout.strip())[:200])
    return report


def replay(source, out=None, pace_ms=DEFAULT_PACE_MS,
           checkpoint_ms=DEFAULT_CHECKPOINT_MS, grace_ms=DEFAULT_GRACE_MS,
           execute=False):
    skill, skill_path = load_skill(source)
    script, hotkeys, checkpoints = build_script(
        skill, pace_ms=pace_ms, checkpoint_ms=checkpoint_ms, grace_ms=grace_ms)
    if not hotkeys and not checkpoints:
        raise ReplayError(
            "%s has no steps to pace — nothing to replay" % skill_path)
    out = Path(out) if out else Path("%s.peekaboo.json" % (skill.get("name") or "replay"))
    written = write_script(script, out)
    report = run_script(written) if execute else None
    return {
        "skill": skill.get("name") or skill_path.stem,
        "script": str(written),
        "hotkeys": hotkeys,
        "checkpoints": checkpoints,
        "report": report,
    }
