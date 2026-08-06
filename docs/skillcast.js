/**
 * skillcast core, ported from the Python package.
 *
 * The rules here are deliberately identical to skillcast/extract.py and
 * skillcast/synth.py. If the demo used looser heuristics it would flatter the
 * tool, and the first thing anyone does after the demo is run the real CLI.
 *
 * Kept dependency-free so it can be read in one sitting.
 */

const PROMPT = /^\s*(?:\$|>|#|❯|➜|PS\s*[^>]*>)\s+(?<cmd>\S.*)$/;

const KNOWN_TOOLS = new Set([
  'npm', 'npx', 'pnpm', 'yarn', 'bun', 'deno', 'node',
  'pip', 'pip3', 'python', 'python3', 'uv', 'poetry', 'pytest',
  'git', 'gh', 'docker', 'kubectl', 'helm', 'terraform',
  'cargo', 'go', 'make', 'brew', 'apt', 'apt-get', 'curl', 'wget',
  'mkdir', 'cd', 'cp', 'mv', 'rm', 'ls', 'cat', 'chmod', 'export',
  'vite', 'next', 'tsc', 'eslint', 'prettier', 'vitest', 'jest',
]);

const OUTPUT_NOISE = /^\s*(added|removed|changed|found|audited|Done|Success|✓|✔|×|✗|error|warn|info|Test Files|Tests|Duration|Start at|PASS|FAIL|\d+ packages?|up to date|npm notice|Scaffolding)/i;

const PATH_LIKE = /[\w.-]+\/[\w./-]+|\b[\w-]+\.(?:json|ts|tsx|js|jsx|py|toml|ya?ml|md|lock|cfg|ini|env)\b/g;
const URL_LIKE = /https?:\/\/[^\s'"<>)\]]+/g;

/**
 * Undo what OCR does to monospaced terminal text.
 *
 * Dashes are the damaging case and they are ambiguous: tesseract draws a lone
 * hyphen and a double hyphen with the same em dash glyph. Measured against a
 * terminal recording:
 *   "my-app -- --template"  reads as  "my-app —- —-template"
 *   "cd my-app"             reads as  "cd my—app"
 */
export function cleanOcrLine(line) {
  line = line
    .replace(/[‘’]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/ /g, ' ');
  line = line.replace(/—-/g, '--');              // "—-template" -> "--template"
  line = line.replace(/(?<=\w)—(?=\w)/g, '-');   // "my—app"     -> "my-app"
  line = line.replace(/—/g, '--');
  line = line.replace(/–/g, '-');
  line = line.replace(/-{3,}(?=[A-Za-z])/g, '--');
  // OCR inserts a space before a file extension: "package. json".
  line = line.replace(
    /(\w)\.\s+(json|jsonc|ts|tsx|js|jsx|mjs|cjs|py|rb|go|rs|toml|ya?ml|md|lock|txt|cfg|ini|env|sh|sql|html|css)\b/gi,
    '$1.$2');
  return line.replace(/\s+$/, '');
}

/**
 * Final repair applied only to text already judged to be a command. Dash runs
 * are collapsed here, not in cleanOcrLine, because output may legitimately
 * contain a "-------" separator while no CLI flag takes three hyphens.
 */
export function repairCommand(command) {
  return command.replace(/-{3,}/g, '--');
}

/** Is this line something a human typed, rather than something printed? */
export function looksLikeCommand(line) {
  line = line.trim();
  if (!line || line.length > 400) return null;
  const prompt = PROMPT.exec(line);
  if (prompt) {
    const candidate = prompt.groups.cmd.trim();
    if (!candidate || OUTPUT_NOISE.test(candidate)) return null;
    return repairCommand(candidate);
  }
  if (OUTPUT_NOISE.test(line)) return null;
  const parts = line.split(/\s+/);
  if (parts.length > 1 && KNOWN_TOOLS.has(parts[0])) return repairCommand(line);
  return null;
}

const STACK_HINTS = [
  [/\bvite\b/, 'Vite'], [/\bnext\b|create-next-app/, 'Next.js'],
  [/\breact\b|react-ts/, 'React'], [/\bvue\b/, 'Vue'], [/\bsvelte\b/, 'Svelte'],
  [/\bvitest\b/, 'Vitest'], [/\bjest\b/, 'Jest'], [/\bpytest\b/, 'pytest'],
  [/\bdocker\b/, 'Docker'], [/\bkubectl\b|\bhelm\b/, 'Kubernetes'],
  [/\bterraform\b/, 'Terraform'], [/\bcargo\b/, 'Rust'],
  [/\bgo (?:run|build|test|mod)\b/, 'Go'],
  [/\bpip\b|\bpython3?\b|\buv\b|\bpoetry\b/, 'Python'],
  [/\bnpm\b|\bpnpm\b|\byarn\b|\bbun\b/, 'Node.js'], [/\bgit\b/, 'Git'],
];

const GLOBS_FOR_STACK = {
  'Node.js': ['**/package.json', '**/*.ts', '**/*.tsx', '**/*.js'],
  React: ['**/*.tsx', '**/*.jsx'],
  Python: ['**/*.py', '**/pyproject.toml', '**/requirements.txt'],
  Rust: ['**/*.rs', '**/Cargo.toml'], Go: ['**/*.go'],
  Docker: ['**/Dockerfile', '**/docker-compose*.y*ml'], Terraform: ['**/*.tf'],
};

export function detectStack(commands) {
  const joined = commands.join(' ').toLowerCase();
  const found = [];
  for (const [pattern, label] of STACK_HINTS) {
    if (pattern.test(joined) && !found.includes(label)) found.push(label);
  }
  return found;
}

export function slugify(text, fallback = 'video-skill') {
  const slug = (text || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-').slice(0, 64).replace(/^-+|-+$/g, '');
  return slug || fallback;
}

const CHROME = /^(step \d+ of \d+|\d+:\d+|untitled|terminal|bash|zsh)$/i;

function titleFor(observation, index) {
  for (const raw of observation.lines) {
    let text = raw.trim();
    if (!text || CHROME.test(text)) continue;
    if (/^[$>#]/.test(text)) continue;
    text = text.replace(/\s*step \d+ of \d+\s*$/i, '').trim();
    if (text.length >= 3 && text.length <= 90) return text;
  }
  if (observation.commands.length) {
    return 'Run ' + observation.commands[0].split(/\s+/).slice(0, 3).join(' ');
  }
  return `Step ${index}`;
}

/** Parse one frame's OCR text into structured observations. */
export function observe(text) {
  const lines = text.split('\n').map(cleanOcrLine).filter((l) => l.trim());
  const commands = [];
  const paths = new Set();
  const urls = new Set();
  for (const line of lines) {
    const found = looksLikeCommand(line);
    if (found) commands.push(found);
    for (const m of line.matchAll(PATH_LIKE)) paths.add(m[0]);
    for (const m of line.matchAll(URL_LIKE)) urls.add(m[0]);
  }
  return { lines, commands, paths: [...paths].sort(), urls: [...urls].sort() };
}

export function buildSkill(observations, source = '') {
  const steps = [];
  const seenCommands = new Set();
  const seenFiles = new Set();

  observations.forEach((observation, i) => {
    const fresh = [];
    for (const command of observation.commands) {
      const key = command.split(/\s+/).join(' ');
      if (!seenCommands.has(key)) { seenCommands.add(key); fresh.push(command); }
    }
    // "Now edit package.json" is a real step with no command in it.
    const newFiles = observation.paths.filter(
      (p) => !seenFiles.has(p) && p.split('/').pop().includes('.') && !p.endsWith('...'));
    newFiles.forEach((f) => seenFiles.add(f));
    if (!fresh.length && !newFiles.length && steps.length) return;

    steps.push({
      title: titleFor(observation, i + 1),
      detail: !fresh.length && newFiles.length
        ? `Edit ${newFiles.map((f) => '`' + f + '`').join(', ')}. The recording shows the file on screen rather than a command.`
        : '',
      commands: fresh,
    });
  });

  const all = steps.flatMap((s) => s.commands);
  const stack = detectStack(all);
  const files = [...new Set(observations.flatMap((o) => o.paths))]
    .filter((p) => p.split('/').pop().includes('.') && !p.endsWith('...')).sort();
  const subject = steps.length ? steps[0].title : 'this workflow';
  const display = subject.trim().replace(/\.$/, '');

  const prerequisites = [];
  if (stack.includes('Node.js')) prerequisites.push('Node.js and a package manager (npm, pnpm or yarn)');
  if (stack.includes('Python')) prerequisites.push('Python 3 and pip');
  if (stack.includes('Docker')) prerequisites.push('Docker running locally');
  if (stack.includes('Rust')) prerequisites.push('Rust and cargo');
  if (stack.includes('Go')) prerequisites.push('Go');

  const globs = [];
  for (const entry of stack) {
    for (const glob of GLOBS_FOR_STACK[entry] || []) {
      if (!globs.includes(glob)) globs.push(glob);
    }
  }

  return {
    name: slugify(display),
    description: `${display}. Use when the task involves ${stack.join(', ') || 'the tools shown'}: this skill carries the exact commands demonstrated in the source recording, in order.`.slice(0, 1024),
    summary: `${steps.length} steps taken from a screen recording. The commands were read off the screen rather than transcribed from narration, so they are the literal text that was run.`,
    prerequisites, steps, files, globs, source,
    urls: [...new Set(observations.flatMap((o) => o.urls))].sort(),
  };
}

function body(skill) {
  const out = [];
  if (skill.summary) out.push(skill.summary + '\n');
  if (skill.prerequisites.length) {
    out.push('## Prerequisites\n', ...skill.prerequisites.map((p) => `- ${p}`), '');
  }
  out.push('## Steps\n');
  skill.steps.forEach((step, i) => {
    out.push(`### ${i + 1}. ${step.title}\n`);
    if (step.detail) out.push(step.detail + '\n');
    if (step.commands.length) out.push('```bash', ...step.commands, '```\n');
  });
  if (skill.files.length) out.push('## Files touched\n', ...skill.files.map((f) => `- \`${f}\``), '');
  if (skill.urls.length) out.push('## References\n', ...skill.urls.map((u) => `- ${u}`), '');
  if (skill.source) {
    out.push('---\n',
      `Extracted from \`${skill.source}\` by skillcast. The commands above were read off the screen, not transcribed from narration — but OCR is not infallible, so read them before running them.`);
  }
  return out.join('\n').replace(/\s+$/, '') + '\n';
}

export function renderClaude(skill) {
  const description = skill.description.replace(/\n/g, ' ').trim();
  return `---\nname: ${skill.name}\ndescription: ${JSON.stringify(description)}\n---\n\n# ${titleCase(skill.name)}\n\n${body(skill)}`;
}

export function renderCursor(skill) {
  return `---\ndescription: ${skill.description.replace(/\n/g, ' ').trim()}\nglobs: ${skill.globs.join(', ')}\nalwaysApply: false\n---\n\n# ${titleCase(skill.name)}\n\n${body(skill)}`;
}

export function renderAgents(skill) {
  return `# ${titleCase(skill.name)}\n\n${skill.description.trim()}\n\n${body(skill)}`;
}

function titleCase(name) {
  return name.split('-').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

/** Checks that mirror skillcast/verify.py. */
const DANGEROUS = [
  [/\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+\/(?:\s|$)/, 'rm -rf on /'],
  [/\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r/, 'recursive force delete'],
  [/\bmkfs\b/, 'filesystem format'],
  [/\bdd\s+.*\bof=\/dev\//, 'raw write to a device'],
  [/curl[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh/, 'pipe from network to shell'],
  [/wget[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh/, 'pipe from network to shell'],
  [/\bsudo\s+rm\b/, 'sudo delete'],
];

export function verify(skill) {
  const findings = [];
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(skill.name)) {
    findings.push({ level: 'error', code: 'STR001', message: `name "${skill.name}" is not lowercase kebab-case` });
  }
  if (!skill.description.trim()) {
    findings.push({ level: 'error', code: 'STR001', message: 'description is empty' });
  }
  if (!skill.steps.some((s) => s.commands.length)) {
    findings.push({ level: 'error', code: 'STR001', message: 'no commands; nothing here is executable' });
  }
  skill.steps.forEach((step, i) => {
    for (const command of step.commands) {
      for (const [pattern, label] of DANGEROUS) {
        if (pattern.test(command)) {
          findings.push({ level: 'error', code: 'SEC001', message: `step ${i + 1} contains a destructive command (${label})` });
          break;
        }
      }
      if (/[—–]/.test(command)) {
        findings.push({ level: 'error', code: 'CMD003', message: `step ${i + 1} still contains a typographic dash; this flag will not run` });
      }
      if (/-{3,}/.test(command)) {
        findings.push({ level: 'error', code: 'CMD004', message: `step ${i + 1} has a run of three or more hyphens; no flag takes three` });
      }
      const quotes = (command.match(/"/g) || []).length;
      if (quotes % 2) {
        findings.push({ level: 'error', code: 'CMD002', message: `step ${i + 1} has an unbalanced quote` });
      }
    }
  });
  return findings;
}
