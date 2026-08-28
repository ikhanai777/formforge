/* ═══════════════════════════════════════════════════════════════
   PERSONNEL — who the agency can put on a job.

   Models are the staff you can assign (GET /v1/models). Toolsets are
   the departments they can be posted to (GET /v1/toolsets). Skills
   are the training record — Hermes writes its own (GET /v1/skills).
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, table, toast, field } from '../ui.js';
import { portraitSVG } from '../sprites.js';
import { employeeName, badgeNumber, titleForTool } from '../adapters.js';
import { store } from '../store.js';

export const personnel = {
  id: 'personnel',
  label: 'Personnel',
  fkey: 'F4',

  render(host) {
    const roster = h('div.roster');
    const departments = h('div');
    const training = h('div');

    function paint() {
      const { models, toolsets, skills } = store.catalog;

      /* ── staff dossiers ───────────────────────────────────── */
      clear(roster);
      if (!models.length) {
        roster.append(h('div.floor-empty', 'no staff on file — the switchboard has not answered GET /v1/models.'));
      }
      for (const m of models) {
        const assigned = String(store.settings.model) === String(m.id);
        roster.append(h('div.dossier',
          h('div.dossier-top',
            h('div.dossier-portrait', { html: portraitSVG(m.id) }),
            h('div',
              h('h3', m.id),
              h('div.title', m.provider ?? 'provider unstated'),
              h('div.tiny.faint', `PERSONNEL FILE ${badgeNumber(m.id)}`))),
          h('dl',
            h('dt', 'Alias'), h('dd', employeeName(m.id)),
            h('dt', 'Context'), h('dd', m.context ? `${m.context.toLocaleString()} tokens` : '—'),
            h('dt', 'Posting'), h('dd', assigned ? h('span.ok', 'ON ASSIGNMENT') : 'available')),
          h('div', { style: { display: 'flex', gap: '4px' } },
            h('button', {
              class: assigned ? 'primary' : '',
              onclick: () => {
                store.saveSettings({ model: assigned ? '' : m.id });
                toast(assigned ? 'returned to the pool' : `${m.id} assigned to new work orders`, 'ok');
                paint();
              },
            }, assigned ? 'Stand down' : 'Assign'))));
      }

      /* ── departments (toolsets) ───────────────────────────── */
      clear(departments);
      if (!toolsets.length) {
        departments.append(h('p.faint', 'GET /v1/toolsets returned nothing — the endpoint is gated by the API key.'));
      }
      for (const t of toolsets) {
        departments.append(h('div', { style: { marginBottom: '10px' } },
          h('div', { style: { display: 'flex', gap: '8px', alignItems: 'baseline' } },
            h('span.hi', t.name.toUpperCase()),
            h('span.tiny', t.enabled ? h('span.ok', 'STAFFED') : h('span.faint', 'CLOSED')),
            h('span.tiny.faint', `${t.tools.length} post${t.tools.length === 1 ? '' : 's'}`)),
          t.description ? h('div.tiny.dim', t.description) : null,
          h('div.certs', { style: { marginTop: '3px' } },
            t.tools.map((tool) => h('span.cert', { 'data-on': t.enabled ? '1' : '0', title: titleForTool(tool) }, tool)))));
      }

      /* ── training record (skills) ─────────────────────────── */
      clear(training);
      training.append(table(
        ['Skill', 'What it does', 'Origin', 'Times used'],
        skills.map((s) => h('tr',
          h('td', h('span.hi', s.name)),
          h('td', h('div.mono-wrap', s.description || '—')),
          h('td', s.source ?? '—'),
          h('td.num', s.uses ?? '—'))),
        'no skills on file. Hermes writes these itself after complex tasks — GET /v1/skills.'));
    }

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Personnel'),
        h('span.sub', 'staff · departments · training record'),
        h('span.spacer'),
        h('button.ghost', {
          onclick: () => store.loadCatalog().then(() => toast('roster refreshed', 'ok'), (e) => toast(e.message, 'error')),
        }, 'Refresh roster')),
      panel('Staff available for assignment', roster),
      h('div.cols.two',
        panel('Departments & posts', departments),
        panel('Training record', training))));

    paint();
    const off = store.on('catalog', paint);
    if (!store.catalog.models.length) store.loadCatalog().catch(() => {});
    return off;
  },
};
