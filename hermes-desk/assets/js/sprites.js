/* ═══════════════════════════════════════════════════════════════
   sprites.js — pixel workers, drawn as inline SVG rects.

   A sprite is a list of equal-length strings; each character is one
   pixel looked up in a palette. Palettes resolve to CSS custom
   properties, so every sprite recolours itself when the phosphor
   theme changes — no image assets, no second set of files.
   ═══════════════════════════════════════════════════════════════ */

import { hashId } from './adapters.js';

/* Character → CSS colour. `.` is transparent and never emitted. */
const PALETTE = {
  o: 'var(--bevel-dk)',   // outline
  s: 'var(--ink-hi)',     // skin / highlight
  h: 'var(--ink-dim)',    // hair
  c: 'var(--ink)',        // clothing
  w: 'var(--ink-hi)',     // collar
  e: 'var(--bevel-dk)',   // eye
  d: 'var(--ink-dim)',    // desk top
  D: 'var(--ink-faint)',  // desk front
  m: 'var(--ink-faint)',  // monitor case
  g: 'var(--ok)',         // monitor screen — overridden per status
  p: 'var(--ink-faint)',  // paperwork
};

/* ── the base scene: one worker, one terminal, one desk (24×14) ── */

const DESK_SCENE = [
  '........................',
  '..........ooooooo.......',
  '.........ohhhhhhho......',
  '.........hsssssssh......',
  '.........hsesssesh......',
  '.ooooooo.hsssssssh......',
  '.ogggggo.hssooossh......',
  '.ogggggo...ooooo........',
  '.ogggggo....oso.........',
  '.ooooooo.occccccco......',
  '...ooo..sccccccccs......',
  '..ooooo...occccccco.pppp',
  'dddddddddddddddddddddddd',
  'DDDDDDDDDDDDDDDDDDDDDDDD',
];

/* Frames are the base scene plus a handful of pixel edits, which is
   far less to maintain than four hand-drawn maps. */
const FRAMES = {
  // hands on the keys, left down
  typeA: [],
  // hands on the keys, right down
  typeB: [[10, 8, 'c'], [11, 9, 's'], [10, 17, 's'], [11, 18, '.']],
  // leaning back, both hands off the desk
  idle:  [[10, 8, 'c'], [10, 17, 'c'], [11, 9, 's'], [11, 17, 's']],
  // one hand raised — reading, waiting, or asking
  think: [[10, 17, '.'], [9, 17, 's'], [8, 17, 's'], [7, 17, 's'], [6, 17, 's']],
};

/* Head variants give the roster faces that differ at a glance. */
const HEADS = {
  0: [],                                                    // plain
  1: [[1, 9, 'o'], [1, 17, 'o'], [2, 9, 'h'], [2, 17, 'h']], // fuller hair
  2: [[3, 10, 'h'], [3, 16, 'h'], [2, 13, 'o']],             // parted
  3: [[2, 10, 'o'], [2, 11, 'o'], [2, 12, 'o'],              // visor
      [2, 13, 'o'], [2, 14, 'o'], [2, 15, 'o'], [2, 16, 'o']],
};

const CLOTH_SHADES = ['var(--ink)', 'var(--ink-dim)', 'var(--ink-hi)'];
const HAIR_SHADES = ['var(--ink-dim)', 'var(--ink-faint)', 'var(--ink)'];

/** Screen colour follows the worker's status lamp. */
const SCREEN_BY_STATUS = {
  running: 'var(--ok)',
  queued: 'var(--cool)',
  waiting_for_approval: 'var(--warn)',
  failed: 'var(--hot)',
  completed: 'var(--ink-faint)',
  stopped: 'var(--ink-faint)',
  cancelled: 'var(--ink-faint)',
};

function applyPatches(rows, patches) {
  const grid = rows.map((r) => r.split(''));
  for (const [y, x, ch] of patches) {
    if (grid[y] && x < grid[y].length) grid[y][x] = ch;
  }
  return grid.map((r) => r.join(''));
}

/**
 * Render a pixel map to an SVG string.
 * Adjacent identical pixels on a row are merged into one rect, which
 * cuts the node count by roughly two thirds.
 */
function toSVG(rows, palette, { className = '' } = {}) {
  const h = rows.length;
  const w = rows[0].length;
  const parts = [];

  for (let y = 0; y < h; y++) {
    let x = 0;
    while (x < w) {
      const ch = rows[y][x];
      if (ch === '.') { x++; continue; }
      let run = 1;
      while (x + run < w && rows[y][x + run] === ch) run++;
      const fill = palette[ch] ?? PALETTE[ch];
      if (fill) parts.push(`<rect x="${x}" y="${y}" width="${run}" height="1" fill="${fill}"/>`);
      x += run;
    }
  }

  return `<svg viewBox="0 0 ${w} ${h}" class="${className}" shape-rendering="crispEdges" `
       + `preserveAspectRatio="xMidYMax meet" aria-hidden="true">${parts.join('')}</svg>`;
}

/** Deterministic per-employee appearance, derived from the run/agent id. */
export function appearance(seed) {
  const h = hashId(seed);
  return {
    head: h % 4,
    cloth: CLOTH_SHADES[(h >>> 3) % CLOTH_SHADES.length],
    hair: HAIR_SHADES[(h >>> 6) % HAIR_SHADES.length],
  };
}

/**
 * A worker at their desk.
 * @param {string} seed    run id / subagent id — fixes the face
 * @param {object} opts
 * @param {'typeA'|'typeB'|'idle'|'think'} [opts.frame]
 * @param {string} [opts.status]  drives the terminal screen colour
 */
export function deskSVG(seed, { frame = 'typeA', status = 'running' } = {}) {
  const look = appearance(seed);
  const rows = applyPatches(DESK_SCENE, [...HEADS[look.head], ...(FRAMES[frame] ?? [])]);
  return toSVG(rows, {
    ...PALETTE,
    c: look.cloth,
    h: look.hair,
    g: SCREEN_BY_STATUS[status] ?? 'var(--ok)',
  }, { className: 'sprite' });
}

/** Head-and-shoulders crop for personnel files. */
export function portraitSVG(seed) {
  const look = appearance(seed);
  const rows = applyPatches(DESK_SCENE, HEADS[look.head])
    .slice(1, 11)
    .map((r) => r.slice(8, 19));
  return toSVG(rows, { ...PALETTE, c: look.cloth, h: look.hair }, { className: 'portrait' });
}

/** The animation cycle a status should play. */
export function frameCycle(status) {
  if (status === 'running') return ['typeA', 'typeB'];
  if (status === 'waiting_for_approval') return ['think', 'idle'];
  if (status === 'queued') return ['idle'];
  return ['idle'];
}

/* ── company mark: a winged monogram, 14×14 ────────────────────── */

const MARK = [
  '..............',
  '...o......o...',
  '..oso....oso..',
  '.osssoooossso.',
  'osssssoosssso.',
  '.ossoccccosso.',
  '..o.oc..co.o..',
  '....occcco....',
  '....oc..co....',
  '....oc..co....',
  '.....occo.....',
  '......oo......',
  '..............',
  '..............',
];

export function markSVG() {
  return toSVG(MARK, PALETTE, { className: 'mark' });
}
