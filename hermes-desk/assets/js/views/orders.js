/* ═══════════════════════════════════════════════════════════════
   WORK ORDERS — raise a run (POST /v1/runs) and keep the ledger of
   every run this dashboard has dispatched.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, lamp, field, select, table, toast, confirmBox, fmtStamp, fmtDuration } from '../ui.js';
import { statusLabel, TERMINAL_STATUSES, employeeName } from '../adapters.js';
import { store, short } from '../store.js';

export const orders = {
  id: 'orders',
  label: 'Work Orders',
  fkey: 'F2',
  badge: () => store.activeRuns.length || null,

  render(host) {
    const ledgerBody = h('div');

    /* ── the order pad ──────────────────────────────────────── */
    const prompt = h('textarea', {
      rows: 6,
      placeholder: 'Describe the job the way you would to a capable colleague.\n\nExample: audit the invoices in ~/inbox, list every total that disagrees with the ledger, and draft a memo naming the suppliers concerned.',
    });

    const modelSel = h('div');
    const workspace = h('input', { type: 'text', placeholder: '/home/you/project  (optional)' });
    const approval = h('input', { type: 'checkbox', checked: store.settings.approvalRequired });
    const submit = h('button.primary', { onclick: raise }, 'Raise work order');

    function paintModels() {
      clear(modelSel);
      const opts = [['', 'gateway default']].concat(store.catalog.models.map((m) => [m.id, `${m.id}${m.provider ? ` · ${m.provider}` : ''}`]));
      modelSel.append(select(opts, store.settings.model, (v) => store.saveSettings({ model: v })));
    }

    async function raise() {
      const text = prompt.value.trim();
      if (!text) { toast('the order needs a description', 'error'); return; }
      submit.disabled = true;
      submit.textContent = 'dispatching…';
      try {
        const ctx = {};
        if (workspace.value.trim()) ctx.workspace = workspace.value.trim();
        const id = await store.dispatch({
          prompt: text,
          context: ctx,
          approvalRequired: approval.checked,
        });
        prompt.value = '';
        toast(`work order ${short(id, 12)} on the floor`, 'ok');
        window.dispatchEvent(new CustomEvent('goto', { detail: 'floor' }));
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        submit.disabled = false;
        submit.textContent = 'Raise work order';
      }
    }

    const pad = panel('Order pad',
      h('div',
        field('The job', prompt, 'Sent as `prompt` on POST /v1/runs.'),
        h('div.cols.two',
          field('Assign to model', modelSel, 'Blank uses the gateway default.'),
          field('Workspace', workspace, 'Passed in `context`; lets the agent read project files.')),
        h('label.field',
          h('span.lbl', 'Sign-off'),
          h('div', approval, ' ', h('span.dim', 'hold the run at `waiting_for_approval` before it acts'))),
        h('div', { style: { display: 'flex', gap: '6px' } },
          submit,
          h('button.ghost', { onclick: () => { prompt.value = ''; } }, 'Clear'))));

    /* ── the ledger ─────────────────────────────────────────── */
    function paintLedger() {
      clear(ledgerBody);
      const rows = store.runList.map((r) => h('tr',
        h('td', lamp(r.status), ' ', h('span.tiny', statusLabel(r.status))),
        h('td',
          h('span.hi', employeeName(r.id)),
          h('span.cell-sub', r.parentId ? `↳ subagent of ${short(r.parentId, 10)}` : short(r.id, 22))),
        h('td', h('div.mono-wrap', truncate(r.prompt, 110)),
          r.tool ? h('span.cell-sub', `last tool: ${r.tool}`) : null),
        h('td', r.model ?? '—'),
        h('td.num', fmtStamp(r.createdAt), h('span.cell-sub', TERMINAL_STATUSES.has(r.status)
          ? fmtDuration(r.createdAt, r.updatedAt)
          : `${fmtDuration(r.createdAt)} elapsed`)),
        h('td', h('div.row-actions',
          h('button.ghost', {
            disabled: TERMINAL_STATUSES.has(r.status),
            onclick: () => store.stop(r.id).then(() => toast('stood down', 'ok'), (e) => toast(e.message, 'error')),
          }, 'Stop'),
          h('button.ghost', {
            onclick: () => confirmBox(`Strike work order ${short(r.id, 16)} from the ledger? The dashboard forgets it; Hermes keeps its own record.`,
              () => store.forgetRun(r.id)),
          }, 'Strike')))));

      ledgerBody.append(table(
        ['Status', 'Assigned to', 'Order', 'Model', 'Raised', ''],
        rows,
        'no work orders raised from this terminal yet.'));
    }

    const ledger = panel('Order ledger',
      ledgerBody,
      h('button.ghost', { onclick: () => { store.clearFiled(); } }, 'Clear filed'));

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Work Orders'),
        h('span.sub', 'POST /v1/runs · the dashboard keeps the ledger, Hermes keeps the run')),
      h('div.cols.side', pad, ledger)));

    paintModels();
    paintLedger();
    const off1 = store.on('runs', paintLedger);
    const off2 = store.on('catalog', paintModels);
    if (!store.catalog.models.length) store.loadCatalog().catch(() => {});

    return () => { off1(); off2(); };
  },
};

function truncate(s, n) {
  const t = String(s ?? '');
  return t.length > n ? `${t.slice(0, n)}…` : t || '—';
}
