# Brief — skillcast: narration track + playlist routes

Date: 2026-08-09 · Owner: Obed · Executor: Claude Code (headless)
Repo: ~/Documents/personal/skillcast · Branch: work on `main` is fine (single-author repo)

## Mission

skillcast today reads the **screen** of a screencast (scene detection → OCR → commands → verified skill). The project's next chapter, from the founder:

> A user pastes a YouTube tutorial — e.g. "how to model a flower in Blender" — and gets a skill
> with everything the video contains: not just the transcript, but what was on screen. And: "I want
> to learn Blender in general" → paste a playlist → get a learning route, from the most general to
> the most particular.

Implement the two features below. They turn skillcast from "terminal screencasts only" into
"any narrated tutorial", while keeping the project's core promise: **never invent steps**.

## Hard rules (do not violate)

1. **Zero runtime dependencies.** `pyproject.toml` stays `dependencies = []`. Stdlib only. yt-dlp
   and ffmpeg/tesseract remain external tools, invoked via subprocess, with clear errors when missing.
2. **Python >= 3.8.** No 3.9+ only syntax (`str.removeprefix`, `X | Y` unions at runtime, etc.).
3. **Never invent steps.** Steps come from subtitle cues / chapters (real narration) or from OCR'd
   commands (real screen). OCR noise alone must still be refused, exactly like today. The existing
   guard in `cli.py` (refuse when candidates exist but none are recognised) must keep passing its tests.
4. **No network in tests.** New tests use fixtures (synthetic VTT files, a saved flat-playlist JSON).
   All 42 existing tests must keep passing: `python3 -m unittest discover -s tests -t .`
5. **Match the codebase's voice.** Read `skillcast/extract.py`, `synth.py`, `verify.py`, `emit.py`,
   `cli.py` and the README before writing. Prose comments are plainspoken and honest about limits.
   Error messages say what happened and the way through. No marketing words.
6. **Keep the CLI exit-code contract** pinned by `tests/test_skillcast.py` (0 ok / 1 extraction or
   verification failure / 2 usage or fetch error). New failure modes must pick the right code.
7. **No API keys, no secrets, no machine-local paths** anywhere.
8. English-first output. Subtitle language: prefer `en`, fall back to the video's native subs,
   note which language was used. (Multi-language polish comes later — design the code so a
   `--sub-langs es,en` flag is a one-line change, but do not build it.)

## Feature A — narration track (new module `skillcast/narrate.py`)

**Fetch.** When the input is a URL, after downloading the video, also fetch subtitles with yt-dlp:
`--write-subs --write-auto-subs --sub-format vtt --skip-download`, preferring manual subs over
auto-generated, `en` first then any. Parse the VTT with a small stdlib parser (cues: start_s,
end_s, text; strip tags/positioning). Local files: skip unless a sidecar `.vtt`/`.srt` sits next
to the video (document this). If yt-dlp reports no subs: narration is simply absent — never an error.

**Enrich runbooks (terminal videos — the current case).** Align cues to each scene's timestamp
window (a cue belongs to a step if it overlaps [scene_t, next_scene_t)). `Step` gains an optional
`narration` field (1–3 sentences, verbatim-light-trimmed, quoted). Emitted into SKILL.md under the
step's commands as `> "…" — 03:12` and into `skill.json`. This is *enrichment*: commands remain the
backbone. Flag: `--narration/--no-narration`, default **on** for URL inputs, off for local files
without a sidecar.

**Guide mode (the Blender-flower case).** When extraction finds **no commands** but narration cues
exist: instead of today's refusal, build a *guide* skill:

- Steps come from narration structure, best source first:
  1. YouTube chapters (already in the yt-dlp info JSON from `fetch.download` — plumb it through).
  2. Else fixed-window segmentation (~60–90s) merged on silence gaps (>2s between cues).
- Each step: title (chapter title, or first cue sentence trimmed to ≤60 chars), timestamp,
  `narration` (the cues in that window, trimmed), plus **`screen` evidence**: the OCR lines captured
  inside that window that are not commands — these are menu names, button labels, values on screen.
  Label them honestly: "Seen on screen". Detect keyboard shortcuts in narration/screen text with a
  conservative regex (`(Ctrl|Cmd|Shift|Alt)\+[A-Z0-9]` and Blender-style single keys said as
  "press G") and collect them per step as "Shortcuts".
- The emitted SKILL.md frontmatter gains `kind: guide` (runbooks are `kind: runbook`).
  Description line for guides: what the viewer will be able to do, from the narration, one sentence.
- Verification for guides: structure + substance checks as today; shell checks do not apply (no
  commands). A guide with zero shortcuts and zero screen evidence gets a warning saying it is
  narration-only and should be reviewed.
- If there are no commands AND no narration → today's refusal path, unchanged.

**README:** update "What it does not do" (narration is no longer absent), add a Guide section with
an honest limits paragraph (guides tell your agent what the tutorial teaches and where to look;
they do not replay clicks — computer-use replay is future work, mentioned in one line under a
"Roadmap" note).

## Feature B — `skillcast route` (new module `skillcast/route.py` + CLI subcommand)

```
skillcast route <playlist-url> ["goal"] [-o route] [--sort playlist|general-first]
          [--limit N] [--narration/--no-narration] [--dry-run] [--json]
```

- List entries via `yt-dlp --flat-playlist --dump-single-json` (subprocess, parse stdout JSON).
  Missing yt-dlp / private playlist / empty → exit 2 with the way through, in the project's voice.
- `--sort playlist` (default) keeps the curator's order. `--sort general-first` is a deterministic
  title heuristic: score each title (intro|beginner|basics|fundamentals|getting started|overview
  → earlier; advanced|pro tips|deep dive|masterclass → later), stable-sort, and print the reordering
  it applied. Tested against a fixture list.
- Run the full pipeline per video (narration + guide support included) into
  `route/<playlist-slug>/<nn>-<video-slug>/`. A video that fails extraction does not kill the route:
  record the failure in ROUTE.md with the reason and continue. `--limit N` caps videos.
- Emit `route/<playlist-slug>/ROUTE.md`: the goal (or the playlist title when no goal given),
  ordered curriculum with duration per video and cumulative time, one-line "what you'll be able to
  do" per video (chapter-1 title or first narration cue or video title, in that priority),
  links to each generated SKILL.md, a failures section, and a final fenced block the user can paste
  to their agent: "Learn this route: <list of skill paths> in order."
- `ROUTE.md` prose follows the founder's copy rule for anything end-user-facing: plain, warm,
  no internal jargon (never "schema", "slug", "OCR" in ROUTE.md — "what was on screen" is fine).

**Tests** (add, don't touch existing): VTT parser (timestamps, tags, multiline cues), cue→scene
alignment, guide-mode synthesis from a narration fixture + OCR observations fixture (assert no
invented commands, assert shortcuts extracted, assert `kind: guide`), the refusal path still firing
when no narration exists, general-first ordering (stable, deterministic), ROUTE.md assembly from a
flat-playlist fixture. Run the whole suite green.

## Deliverables checklist

- [ ] `skillcast/narrate.py`, `skillcast/route.py`, CLI wiring, version bump to `0.2.0`
- [ ] New tests green + all 42 existing tests green
- [ ] README: narration section, guide section, route section with a real-shaped example, roadmap line
- [ ] `docs/prompts/` brief left in the repo (this file — it tells the story, keep it)
- [ ] Commit(s) with conventional messages; do NOT push (the parent agent pushes after live certification)

## Live certification (the parent agent does this — do not attempt network runs)

A real Blender tutorial URL and a real Blender playlist will be run against the built CLI by the
orchestrating agent. Write the code so that first contact with a real 10-minute 1080p Blender
tutorial is boring: bounded frame count, bounded subtitle size, timeouts on every subprocess.
