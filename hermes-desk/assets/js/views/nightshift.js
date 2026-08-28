/* ═══════════════════════════════════════════════════════════════
   NIGHT SHIFT — work that runs without anyone at the desk.
   Backed by the Hermes jobs API (/api/jobs), which is the REST face
   of the agent's built-in cron scheduler.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, table, field, lamp, toast, confirmBox, fmtStamp, fmtAgo } from '../ui.js';
import { readJobs } from '../adapters.js';
import { store, short } from '../store.js';

const PRESETS = [
  ['0 8 * * 1', 'Mondays, 08:00'],
  ['0 7 * * 1-5', 'Weekdays, 07:00'],
  ['0 * * * *', 'Hourly, on the hour'],
  ['30 2 * * *', 'Nightly, 02:30'],
  ['0 6 1 * *', 'First of the month, 06:00'],
];

export const nightshift = {
  id: 'nightshift',
  label: 'Night Shift',
  fkey: 'F6',

  render(host) {
    const body = h('div');
    let cache = [];

    /* ── rota form ──────────────────────────────────────────── */
    const name = h('input', { type: 'text', placeholder: 'Monday digest' });
    const cron = h('input', { type: 'text', placeholder: '0 8 * * 1', value: '0 8 * * 1' });
    const prompt = h('textarea', { rows: 4, placeholder: 'What should the agent do, unattended, when this fires?' });

    const presets = h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '4px' } },
      PRESETS.map(([expr, label]) =>
        h('button.chip', { onclick: () => { cron.value = expr; } }, label)));

    async function create() {
      if (!prompt.value.trim()) { toast('the shift needs instructions', 'error'); return; }
      try {
        await store.client.createJob({
          name: name.value.trim() || 'unnamed shift',
          schedule: cron.value.trim(),
          cron: cron.value.trim(),
          prompt: prompt.value.trim(),
          enabled: true,
        });
        name.value = ''; prompt.value = '';
        toast('shift added to the rota', 'ok');
        load();
      } catch (err) { toast(err.message, 'error'); }
    }

    const form = panel('Add to the rota',
      h('div',
        field('Shift name', name),
        field('Schedule', h('div', cron, h('div', { style: { marginTop: '4px' } }, presets)),
          'Standard five-field cron, as the agent\'s own scheduler reads it.'),
        field('Standing instructions', prompt),
        h('button.primary', { onclick: create }, 'Post the shift')));

    /* ── the rota ───────────────────────────────────────────── */
    async function load() {
      clear(body);
      body.append(h('p.faint', 'reading the rota…'));
      try {
        cache = readJobs(await store.client.listJobs());
        paint();
      } catch (err) {
        clear(body);
        body.append(h('p.hot.mono-wrap', `GET /api/jobs failed: ${err.message}`));
      }
    }

    function paint() {
      const rows = cache.map((j) => h('tr',
        h('td', lamp(j.enabled ? 'on' : 'off'), ' ', h('span.tiny', j.enabled ? 'ON ROTA' : 'STOOD DOWN')),
        h('td', h('span.hi', j.name), h('span.cell-sub', short(j.id, 20))),
        h('td', h('code', j.schedule)),
        h('td', h('div.mono-wrap', j.prompt.length > 90 ? `${j.prompt.slice(0, 90)}…` : j.prompt)),
        h('td', fmtAgo(j.lastRun), h('span.cell-sub', j.nextRun ? `next ${fmtStamp(j.nextRun)}` : 'not scheduled')),
        h('td', h('div.row-actions',
          h('button.ghost', { onclick: () => act(j.enabled ? 'pauseJob' : 'resumeJob', j) }, j.enabled ? 'Pause' : 'Resume'),
          h('button.ghost', { onclick: () => act('runJob', j, 'fired now') }, 'Run now'),
          h('button.ghost', {
            onclick: () => confirmBox(`Remove "${j.name}" from the rota permanently?`,
              () => act('deleteJob', j, 'removed')),
          }, 'Remove')))));

      clear(body);
      body.append(table(
        ['Status', 'Shift', 'Schedule', 'Standing instructions', 'Last / next', ''],
        rows,
        'nothing on the rota. Unattended work goes here.'));
    }

    async function act(method, job, msg) {
      try {
        await store.client[method](job.id);
        toast(msg ?? 'rota updated', 'ok');
        load();
      } catch (err) { toast(err.message, 'error'); }
    }

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Night Shift'),
        h('span.sub', '/api/jobs · the agent\'s cron scheduler'),
        h('span.spacer'),
        h('button.ghost', { onclick: load }, 'Refresh')),
      h('div.cols.side', panel('The rota', body), form)));

    load();
    return () => {};
  },
};
