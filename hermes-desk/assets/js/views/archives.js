/* ═══════════════════════════════════════════════════════════════
   ARCHIVES — the filing room. Every session Hermes holds, with its
   transcript, and the ability to branch one (POST .../fork).
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, table, modal, toast, confirmBox, fmtStamp, fmtAgo } from '../ui.js';
import { readSessions, readMessages } from '../adapters.js';
import { store, short } from '../store.js';

export const archives = {
  id: 'archives',
  label: 'Archives',
  fkey: 'F5',

  render(host) {
    const body = h('div');
    const filter = h('input', { type: 'text', placeholder: 'filter by title or id…', oninput: () => paint() });
    let cache = [];

    async function load() {
      clear(body);
      body.append(h('p.faint', 'pulling the drawer…'));
      try {
        cache = readSessions(await store.client.listSessions({ limit: 100 }));
        paint();
      } catch (err) {
        clear(body);
        body.append(h('p.hot.mono-wrap', `GET /api/sessions failed: ${err.message}`));
      }
    }

    function paint() {
      const q = filter.value.trim().toLowerCase();
      const rows = cache
        .filter((s) => !q || s.title.toLowerCase().includes(q) || s.id.toLowerCase().includes(q))
        .map((s) => h('tr',
          h('td', h('span.hi', s.title), h('span.cell-sub', short(s.id, 26))),
          h('td.num', s.messages ?? '—'),
          h('td', fmtStamp(s.created), h('span.cell-sub', fmtAgo(s.created))),
          h('td', fmtStamp(s.updated), h('span.cell-sub', fmtAgo(s.updated))),
          h('td', h('div.row-actions',
            h('button.ghost', { onclick: () => openTranscript(s) }, 'Read'),
            h('button.ghost', { onclick: () => fork(s) }, 'Branch'),
            h('button.ghost', {
              onclick: () => confirmBox(`Destroy session "${s.title}"? This asks Hermes to delete it, not just this view.`,
                async () => {
                  try { await store.client.deleteSession(s.id); toast('session destroyed', 'ok'); load(); }
                  catch (e) { toast(e.message, 'error'); }
                }),
            }, 'Destroy')))));

      clear(body);
      body.append(table(
        ['Session', 'Msgs', 'Opened', 'Last touched', ''],
        rows,
        cache.length ? 'nothing matches that filter.' : 'the drawer is empty.'));
    }

    async function fork(s) {
      try {
        const res = await store.client.forkSession(s.id);
        toast(`branched to ${short(res?.id ?? '?', 14)}`, 'ok');
        load();
      } catch (err) { toast(err.message, 'error'); }
    }

    async function openTranscript(s) {
      const pane = h('div.transcript', { style: { height: '52vh' } }, h('p.faint', 'reading…'));
      modal({ title: `Session · ${s.title}`, body: pane, actions: [] });
      try {
        const msgs = readMessages(await store.client.sessionMessages(s.id));
        clear(pane);
        if (!msgs.length) pane.append(h('p.faint', 'no messages recorded.'));
        for (const m of msgs) {
          pane.append(h('div.msg', { 'data-role': m.role === 'user' ? 'user' : 'assistant' },
            h('div.who', `${m.role}${m.at ? ` · ${fmtStamp(m.at)}` : ''}`),
            h('div.body', m.content)));
        }
      } catch (err) {
        clear(pane);
        pane.append(h('p.hot.mono-wrap', err.message));
      }
    }

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Archives'),
        h('span.sub', 'GET /api/sessions · every thread the agent remembers'),
        h('span.spacer'),
        h('div', { style: { width: '240px' } }, filter),
        h('button.ghost', {
          onclick: async () => {
            try { await store.client.createSession({ title: 'Opened from archives' }); toast('drawer opened', 'ok'); load(); }
            catch (e) { toast(e.message, 'error'); }
          },
        }, 'New session'),
        h('button.ghost', { onclick: load }, 'Refresh')),
      panel('Filing room', body)));

    load();
    return () => {};
  },
};
