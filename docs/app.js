/**
 * Browser demo wiring.
 *
 * ffmpeg is not available here, so scene detection is done directly on canvas:
 * seek, draw, compare against the previous frame, keep the ones that changed.
 * The threshold is calibrated to the same finding as the CLI — a terminal
 * advancing moves very few pixels — so it is deliberately low.
 */
import { buildSkill, knownToolCommands, observe, renderAgents, renderClaude,
  renderCursor, verify } from './skillcast.js';

const el = (id) => document.getElementById(id);
const drop = el('drop');
const fileInput = el('file');
const progress = el('progress');
const fill = el('fill');
const status = el('status');
const thumbs = el('thumbs');
const result = el('result');
const output = el('output');
const findingsBox = el('findings');

let rendered = { claude: '', cursor: '', agents: '' };
let current = 'claude';

function setProgress(pct, message) {
  progress.classList.add('on');
  fill.style.width = `${Math.round(pct * 100)}%`;
  status.textContent = message;
}

/**
 * Sample the video and keep frames that differ from the one before.
 *
 * Downscaled greyscale mean-absolute-difference. A terminal advancing by one
 * line changes a tiny fraction of pixels, so the bar has to sit low: 0.4% of
 * the maximum possible difference, found by the same measurement that set the
 * CLI's ffmpeg threshold.
 */
async function extractFrames(video, onProgress) {
  const SAMPLE_EVERY = 0.5;         // seconds
  const DIFF_THRESHOLD = 0.004;
  const MAX_FRAMES = 24;            // keeps the demo responsive

  const canvas = document.createElement('canvas');
  const width = Math.min(video.videoWidth || 1280, 1280);
  const scale = width / (video.videoWidth || 1280);
  canvas.width = width;
  canvas.height = Math.round((video.videoHeight || 720) * scale);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  // A small grid is plenty to tell "the screen changed" from "it did not".
  const probe = document.createElement('canvas');
  probe.width = 64; probe.height = 36;
  const pctx = probe.getContext('2d', { willReadFrequently: true });

  const seek = (t) => new Promise((resolve) => {
    const done = () => { video.removeEventListener('seeked', done); resolve(); };
    video.addEventListener('seeked', done);
    video.currentTime = Math.min(t, Math.max(0, video.duration - 0.05));
  });

  const frames = [];
  let previous = null;
  const total = Math.max(1, Math.floor(video.duration / SAMPLE_EVERY));

  for (let i = 0; i <= total && frames.length < MAX_FRAMES; i += 1) {
    await seek(i * SAMPLE_EVERY);
    pctx.drawImage(video, 0, 0, probe.width, probe.height);
    const data = pctx.getImageData(0, 0, probe.width, probe.height).data;

    let diff = 0;
    if (previous) {
      let sum = 0;
      for (let p = 0; p < data.length; p += 4) {
        const a = (data[p] + data[p + 1] + data[p + 2]) / 3;
        const b = (previous[p] + previous[p + 1] + previous[p + 2]) / 3;
        sum += Math.abs(a - b);
      }
      diff = sum / (data.length / 4) / 255;
    }

    if (!previous || diff > DIFF_THRESHOLD) {
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      frames.push(canvas.toDataURL('image/png'));
    }
    previous = data;
    onProgress(i / (total + 1));
  }
  return frames;
}

async function run(file, label) {
  result.classList.remove('on');
  thumbs.innerHTML = '';
  findingsBox.innerHTML = '';
  setProgress(0.02, 'loading video…');

  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.src = URL.createObjectURL(file);

  try {
    await new Promise((resolve, reject) => {
      video.addEventListener('loadedmetadata', resolve, { once: true });
      video.addEventListener('error', () => reject(new Error('that file could not be read as video')), { once: true });
    });

    setProgress(0.06, `scanning ${video.duration.toFixed(0)}s for scene changes…`);
    const frames = await extractFrames(video, (p) => {
      setProgress(0.06 + p * 0.3, `scanning for scene changes… ${Math.round(p * 100)}%`);
    });

    frames.slice(0, 8).forEach((src) => {
      const img = document.createElement('img');
      img.src = src;
      thumbs.appendChild(img);
    });
    setProgress(0.38, `${frames.length} scene changes · starting OCR (first run downloads the language data)`);

    const worker = await Tesseract.createWorker('eng');
    const observations = [];
    for (let i = 0; i < frames.length; i += 1) {
      const { data } = await worker.recognize(frames[i]);
      observations.push(observe(data.text));
      setProgress(0.38 + ((i + 1) / frames.length) * 0.58,
        `reading frame ${i + 1} of ${frames.length}…`);
    }
    await worker.terminate();

    const skill = buildSkill(observations, label);
    const commandCount = skill.steps.reduce((n, s) => n + s.commands.length, 0);

    // Footage with no terminal still yields prompt-shaped OCR noise. Refuse it
    // rather than showing a skill made of stray characters.
    const recognised = knownToolCommands(observations);
    if (commandCount && !recognised.length) {
      setProgress(1, 'not a screen recording');
      findingsBox.innerHTML =
        `<div class="f error">This does not look like a screen recording. ` +
        `${commandCount} candidate command(s) were found but not one uses a program ` +
        `I recognise — that is what OCR noise looks like, not anything that was typed. ` +
        `skillcast reads terminals and editors; a video of hands, slides or a talking ` +
        `head has nothing for it to lift.</div>`;
      result.classList.add('on');
      output.textContent = '';
      return;
    }

    if (!commandCount) {
      setProgress(1, 'no commands found on screen');
      findingsBox.innerHTML =
        '<div class="f error">No commands were found. The recording may not show a terminal, ' +
        'or the on-screen type may be too small — below roughly 16px the OCR starts dropping lines.</div>';
      result.classList.add('on');
      output.textContent = '';
      return;
    }

    rendered = {
      claude: renderClaude(skill),
      cursor: renderCursor(skill),
      agents: renderAgents(skill),
    };
    show(current);

    const findings = verify(skill);
    findingsBox.innerHTML = findings.length
      ? findings.map((f) => `<div class="f error">${f.code} — ${f.message}</div>`).join('')
      : `<div class="f ok">verified · ${skill.steps.length} steps · ${commandCount} commands · no problems found</div>`;

    setProgress(1, `done — ${skill.steps.length} steps, ${commandCount} commands`);
    result.classList.add('on');
  } catch (error) {
    setProgress(1, 'failed');
    findingsBox.innerHTML = `<div class="f error">${error.message}</div>`;
    result.classList.add('on');
  } finally {
    URL.revokeObjectURL(video.src);
  }
}

function show(target) {
  current = target;
  output.textContent = rendered[target] || '';
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('on', tab.dataset.t === target);
  });
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.t));
});

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => {
  if (e.target.files[0]) run(e.target.files[0], e.target.files[0].name);
});
['dragover', 'dragenter'].forEach((type) =>
  drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((type) =>
  drop.addEventListener(type, () => drop.classList.remove('over')));
drop.addEventListener('drop', (e) => {
  e.preventDefault();
  const file = [...e.dataTransfer.files].find((f) => f.type.startsWith('video/'));
  if (file) run(file, file.name);
});

el('sample').addEventListener('click', async () => {
  setProgress(0.01, 'fetching the sample…');
  const response = await fetch('./tutorial.mp4');
  run(await response.blob(), 'tutorial.mp4');
});

el('copy').addEventListener('click', async () => {
  await navigator.clipboard.writeText(rendered[current]);
  el('copy').textContent = 'Copied';
  setTimeout(() => { el('copy').textContent = 'Copy'; }, 1400);
});

el('download').addEventListener('click', () => {
  const names = { claude: 'SKILL.md', cursor: 'skill.mdc', agents: 'AGENTS.md' };
  const blob = new Blob([rendered[current]], { type: 'text/markdown' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = names[current];
  link.click();
  URL.revokeObjectURL(link.href);
});
