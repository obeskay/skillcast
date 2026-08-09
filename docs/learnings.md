# The first real YouTube video broke my CLI five ways

*Cross-post draft for dev.to / Medium. Canonical home: this repo.*

---

I built skillcast to read the screen of a tutorial video and emit a skill a
coding agent can run. No transcript, no API key, no model. Scene detection,
OCR, and the stubborn belief that the characters on screen are the only ones
that actually execute.

Fifty unit tests passed. Then I pointed it at a real Blender tutorial on
YouTube and it broke five different ways in the first ten minutes.

That run is the whole point of this post. The tests were green. The tool was
wrong. Every bug below was invisible until real video touched it, and each one
now has a regression test named after it.

## Bug 1: the URL path crashed before doing any work

`UnboundLocalError: local variable 'info' referenced before assignment.` A
variable got renamed during a refactor and one line kept the old name. Every
URL run died instantly; local files worked fine.

No test caught it because the test suite never touches the network. That is a
deliberate rule (tests must run offline) with a sharp edge: the only code path
most users will ever take had zero coverage. The fix was one word. The lesson
cost me a full download of a Blender video to learn: an offline suite needs a
live certification harness next to it, not instead of it. Now there is a
script that runs the real thing against real videos before anything ships.

## Bug 2: the new feature was unreachable

The release added guide skills for narrated tutorials, the Blender-flower
case. Fed the official Blender Fundamentals playlist, every single video came
back refused: "candidate commands were found, but none uses a recognised
program."

That refusal is a guard I wrote and still believe in. OCR of a Blender
viewport produces glyph soup, and soup shaped like `$ rm -rf` must never
become a skill. But the guard ran *before* the guide branch. Any video with
both narration and noisy OCR lost. Real commands beat narration, narration
beats OCR noise; the guard only fires when neither exists. One reordering,
three videos converted.

## Bug 3: YouTube's auto-captions say everything three times

The first guide read: *"Hi, here's a quick tutorial about how to Hi, here's a
quick tutorial about how to make a low poly flower in Blender. I'm make a low
poly flower in Blender."*

YouTube auto-captions arrive as rolling windows. Each cue repeats the previous
cue's tail and adds a few words. Concatenate naively and every sentence
triplicates. The fix is a word-level overlap dedupe: strip the longest
head-to-tail match between consecutive cues. Manual subtitles have no overlap,
so it costs them nothing. The guide now reads like the person talks.

## Bug 4: `<Untitled Chapter 1>` is not a title

YouTube fills untitled chapters with the placeholder `<Untitled Chapter 1>`.
My pipeline stripped "HTML tags" from text before using it. Angle brackets
are angle brackets: the placeholder vanished, the title came out empty, and
one guide shipped with the description *"Follow the narration to ."* — a
sentence that trails off into nothing.

Placeholders are now recognised and treated as absent, so the title falls
through to the first real sentence the narrator says. Small fix, embarrassing
class of bug. Anything your cleanup code can eat, it eventually will.

## Bug 5: 720p OCR is confident garbage

The guide's "Seen on screen" section, meant to capture menu labels, came out
as `« Ce oe OO hh OOO R BS OF Firanctwin 9 8 40% 8`. Tesseract reads a 720p
Blender viewport the way I read a menu across a dark restaurant: with
confidence and no accuracy.

The honest options were a vocabulary filter or dropping the section. I did
both. On-screen evidence now has to contain a real interface word — File,
Object, Modifier, Timeline, the chrome of almost any GUI — before it earns a
bullet. Most guides end up narration-plus-shortcuts, and they say so. Claiming
less turned out to be the feature.

## What shipped anyway

The flower tutorial became a five-step guide with the chapters YouTube gave
it, the shortcuts the narrator spoke (*"pressing Ctrl and R"* became
`Ctrl+R`), and an honest footer saying it does not replay clicks. The
playlist became a `ROUTE.md`: ordered curriculum, cumulative time, what you
will be able to do after each video, and a block you paste to your agent.

Then, because guides know which keys were pressed and when, I taught the CLI
to hand them back to the machine. `skillcast replay` writes a Peekaboo
script: each shortcut a hotkey step, each keyless step a pause where you do
the clicking, paced like the video. It presses keys. It does not click, and it
says why: nothing in a recording tells it where *your* buttons are.

One more live-probe story there. Peekaboo's docs show a flat script format.
The installed build (3.0.0) rejected it; the error messages themselves
revealed the older enum-wrapped shape it actually decodes. A dozen probe
scripts against the real binary beat an afternoon of reading docs for a
version I do not have.

## The numbers, for honesty

- 65 tests, all offline, all green. Zero runtime dependencies. stdlib only.
- 5 release-blocking bugs found only by live runs, each now a regression test.
- 1 YouTube flower modeled, 3 Blender Fundamentals videos routed, 0 invented
  commands.

The standing rule after all this: unit tests prove the machine works. Only
pointing it at the real world proves it works on the world. Certify live or
ship lies.

skillcast is MIT, free, and installs with
`pip install git+https://github.com/obeskay/skillcast`. Issues with a link to
a public video are the most useful thing you can file.
