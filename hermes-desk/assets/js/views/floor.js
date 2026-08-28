/* ═══════════════════════════════════════════════════════════════
   THE FLOOR — every live run is a worker at a desk; every subagent
   spawned by delegate_task is a desk reporting into its lead.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, lamp, toast, modal, fmtDuration, confirmBox } from '../ui.js';
import { deskSVG, frameCycle } from '../sprites.js';
import { statusLabel, TERMINAL_STATUSES, employeeName, badgeNumber } from '../adapters.js';
import { store, short } from '../store.js';

const SCREEN_COLS = 26;

export const floor = {
  id: 'floor',
  label: 'The Floor',
  fkey: 'F1',

  render(host) {
    const gauges = h('div.floor-strip');
    const teams = h('div.teams');
    const desks = new Map(); // runId -> {node, sprite, screen, meter, elapsed, phase, scroll}

    const view = h('div.view',
      h('div.view-head',
        h('h2', 'The Floor'),
        h('span.sub', 'live runs · delegated subagents · tool progress'),
        h('span.spacer'),
        h('button.ghost', { onclick: () => store.clearFiled() }, 'Clear filed'),
        h('button.primary', { onclick: () => window.dispatchEvent(new CustomEvent('goto', { detail: 'orders' })) }, '+ Work order')),
      gauges,
      teams);

    host.append(view);

    /* Events arrive several times a second. Rebuilding the floor on each
       one would restart every marquee and animation, so the structural
       redraw is gated on the shape of the floor actually changing; the
       per-frame ticker below carries everything else. */
    let signature = '';

    function maybeDraw() {
      const next = store.runList.map((r) => `${r.id}:${r.status}:${r.parentId ?? ''}`).join('|');
      drawGauges(gauges);
      if (next === signature) return;
      signature = next;
      draw();
    }

    function draw() {
      desks.clear();
      clear(teams);

      const groups = store.teams;
      if (!groups.length) {
        teams.append(h('div.floor-empty',
          h('div', '▚▚▚  T H E   F L O O R   I S   E M P T Y  ▚▚▚'), h('br'),
          h('div.tiny', 'no work orders on the books.'),
          h('div.tiny', 'raise one in WORK ORDERS (F2) and the bullpen fills up.')));
        return;
      }

      for (const { lead, crew } of groups) {
        const body = h('div.team-body');
        body.append(deskNode(lead, false, desks));
        for (const member of crew) body.append(deskNode(member, true, desks));

        teams.append(h('div.team',
          h('div.team-head',
            lamp(lead.status),
            h('span.th-title', `TEAM ${badgeNumber(lead.id)}`),
            h('span', '·'),
            h('span', statusLabel(lead.status)),
            h('span.spacer'),
            h('span', crew.length ? `${crew.length} SUBAGENT${crew.length > 1 ? 'S' : ''}` : 'SOLO'),
            h('span', '·'),
            h('span', short(lead.id, 14))),
          body));
      }
    }

    /* ── cheap per-frame update: sprites, tickers, clocks ───────── */
    function tick() {
      for (const [id, d] of desks) {
        const run = store.runs.get(id);
        if (!run) continue;

        const cycle = frameCycle(run.status);
        d.phase = (d.phase + 1) % cycle.length;
        d.sprite.innerHTML = deskSVG(id, { frame: cycle[d.phase], status: run.status });

        d.screen.textContent = marquee(screenText(run), d);
        d.role.textContent = run.title || 'GENERAL CLERK';
        d.meter.style.width = `${progress(run)}%`;
        d.elapsed.textContent = elapsedOf(run);
      }
    }

    maybeDraw();
    const timer = setInterval(tick, 620);
    const offRuns = store.on('runs', maybeDraw);

    return () => { clearInterval(timer); offRuns(); };
  },
};

/* ── one desk ─────────────────────────────────────────────────── */

function deskNode(run, isChild, registry) {
  const status = run.status;
  const sprite = h('div.desk-sprite', { html: deskSVG(run.id, { frame: 'typeA', status }) });
  const entry = { phase: 0, scroll: 0 };
  const screen = h('div.desk-screen', marquee(screenText(run), entry));
  const bar = h('i', { style: { width: `${progress(run)}%` } });
  const elapsed = h('span.elapsed', elapsedOf(run));
  const role = h('div.desk-role', run.title || 'GENERAL CLERK');

  const stopBtn = h('button.danger', {
    disabled: TERMINAL_STATUSES.has(status),
    onclick: () => confirmBox(`Stand ${employeeName(run.id)} down? The run is interrupted.`,
      () => store.stop(run.id).then(
        () => toast('stood down', 'ok'),
        (e) => toast(e.message, 'error'))),
  }, 'Stop');

  const steerBtn = h('button', {
    disabled: TERMINAL_STATUSES.has(status),
    onclick: () => steerDialog(run),
  }, 'Steer');

  const node = h('div.desk', { 'data-status': status, 'data-child': isChild ? '1' : '0' },
    h('div.desk-head',
      lamp(status),
      h('span.station', isChild ? '↳ SUBAGENT' : `DESK ${badgeNumber(run.id)}`),
      h('span.spacer'),
      h('span', statusLabel(status))),
    h('div.desk-body',
      sprite,
      h('div.desk-id',
        h('div.desk-name', employeeName(run.id)),
        role,
        h('div.desk-goal', run.prompt || '—'))),
    screen,
    h('div.desk-meter', bar),
    h('div.desk-foot',
      status === 'waiting_for_approval'
        ? [h('button.primary', { onclick: () => store.approve(run.id, true).then(() => toast('signed off', 'ok')) }, 'Sign'),
           h('button.danger', { onclick: () => store.approve(run.id, false) }, 'Refuse')]
        : [steerBtn, stopBtn],
      h('button.ghost', { onclick: () => logDialog(run) }, 'Log'),
      elapsed));

  Object.assign(entry, { node, sprite, screen, role, meter: bar, elapsed });
  registry.set(run.id, entry);
  return node;
}

function elapsedOf(run) {
  return TERMINAL_STATUSES.has(run.status)
    ? fmtDuration(run.createdAt, run.updatedAt)
    : fmtDuration(run.createdAt);
}

/* ── desk screen ──────────────────────────────────────────────── */

function screenText(run) {
  if (run.status === 'queued') return 'awaiting assignment';
  if (run.status === 'waiting_for_approval') return run.note || 'needs a signature';
  if (run.status === 'failed') return `ERR ${run.error || 'unspecified failure'}`;
  if (TERMINAL_STATUSES.has(run.status)) return run.result || 'work filed';
  const parts = [run.tool, run.note].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'thinking';
}

/** Text longer than the screen scrolls, one column per frame. */
function marquee(text, d) {
  const s = `${text}    `;
  if (s.length <= SCREEN_COLS) { d.scroll = 0; return text.padEnd(SCREEN_COLS, ' ') + '█'; }
  d.scroll = (d.scroll + 1) % s.length;
  return (s + s).slice(d.scroll, d.scroll + SCREEN_COLS) + '█';
}

function progress(run) {
  if (TERMINAL_STATUSES.has(run.status)) return 100;
  if (run.iteration != null && run.maxIterations) {
    return Math.min(98, Math.round((run.iteration / run.maxIterations) * 100));
  }
  return run.status === 'queued' ? 4 : 45;
}

/* ── dialogs ──────────────────────────────────────────────────── */

function steerDialog(run) {
  const input = h('textarea', { rows: 4, placeholder: 'A word in their ear — extra context, a correction, a narrowing of scope.' });
  modal({
    title: `Steer ${employeeName(run.id)}`,
    body: h('div', h('p.tiny.dim', `POST /v1/runs/${short(run.id, 18)}/steer`), input),
    actions: [{
      label: 'Send guidance',
      kind: 'primary',
      onClick: () => {
        const text = input.value.trim();
        if (!text) return;
        store.steer(run.id, text).then(
          () => toast('guidance delivered', 'ok'),
          (e) => toast(e.message, 'error'));
      },
    }],
  });
  setTimeout(() => input.focus(), 0);
}

function logDialog(run) {
  const body = h('div.log', { style: { height: '48vh' } });
  const paint = () => {
    clear(body);
    const lines = store.runs.get(run.id)?.log ?? [];
    if (!lines.length) body.append(h('div.log-line.faint', 'no events recorded on this connection yet.'));
    for (const l of lines) {
      body.append(h('div.log-line', h('span.ts', `${new Date(l.at).toLocaleTimeString()}  `), l.line));
    }
    body.scrollTop = body.scrollHeight;
  };
  paint();
  const off = store.on('runlog', (id) => { if (id === run.id) paint(); });

  modal({
    title: `Day book · ${short(run.id, 20)}`,
    body: h('div',
      h('p.tiny.dim', `GET /v1/runs/${short(run.id, 18)}/events  (server-sent events)`),
      body,
      run.result ? h('div', h('hr.rule'), h('div.tiny.dim', 'RESULT'), h('div.mono-wrap', run.result)) : null,
      run.error ? h('div', h('hr.rule'), h('div.tiny.hot', 'ERROR'), h('div.mono-wrap.hot', run.error)) : null),
    actions: [],
    onClose: off,
  });
}

/* ── the strip of gauges along the top ────────────────────────── */

function drawGauges(host) {
  clear(host);
  const all = store.runList;
  const working = all.filter((r) => r.status === 'running').length;
  const queued = all.filter((r) => r.status === 'queued').length;
  const blocked = all.filter((r) => r.status === 'waiting_for_approval').length;
  const subagents = all.filter((r) => r.parentId).length;
  const filed = all.filter((r) => r.status === 'completed').length;
  const failed = all.filter((r) => r.status === 'failed').length;

  const g = (label, value, note, cls = '') =>
    h('div.gauge', h('div.g-label', label), h('div.g-value', { class: cls }, String(value)), h('div.g-note', note));

  host.append(
    g('AT WORK', working, 'runs in progress', working ? 'ok' : ''),
    g('CLOCKED IN', queued, 'queued'),
    g('SUBAGENTS', subagents, 'delegated desks', 'cool'),
    g('SIGN-OFF', blocked, 'awaiting approval', blocked ? 'warn' : ''),
    g('FILED', filed, 'completed today'),
    g('ERRORED', failed, 'needs review', failed ? 'hot' : ''));
}
