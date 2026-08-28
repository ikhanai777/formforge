/* ═══════════════════════════════════════════════════════════════
   SWITCHBOARD — where the dashboard is wired to a Hermes gateway,
   and where you can see what that gateway says it can do.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, field, select, lamp, toast, fmtStamp } from '../ui.js';
import { store } from '../store.js';

const TRANSPORTS = [
  ['proxy', 'Proxy — via server.py (recommended)'],
  ['direct', 'Direct — browser to gateway'],
  ['simulation', 'Simulation — no gateway'],
];

export const switchboard = {
  id: 'switchboard',
  label: 'Switchboard',
  fkey: 'F7',

  render(host) {
    const wiring = h('div');
    const status = h('div');
    const caps = h('div');
    const logBox = h('div.log');

    /* ── wiring form ────────────────────────────────────────── */
    function paintWiring() {
      clear(wiring);
      const s = store.settings;

      const transport = select(TRANSPORTS, s.transport, (v) => {
        store.saveSettings({ transport: v });
        paintWiring();
        probe();
      });

      const direct = s.transport === 'direct';

      // Element.append() stringifies null, so the optional rows are
      // filtered out rather than passed through as blanks.
      const rows = [
        field('Transport', transport, transportNote(s.transport)),

        direct ? field('Gateway base URL',
          h('input', { type: 'text', value: s.baseUrl, placeholder: 'http://127.0.0.1:8642',
            onchange: (e) => store.saveSettings({ baseUrl: e.target.value.trim() }) }),
          'The API server listens on 127.0.0.1:8642 unless API_SERVER_HOST/PORT say otherwise.') : null,

        direct ? field('API_SERVER_KEY',
          h('input', { type: 'password', value: s.apiKey, placeholder: 'bearer token',
            onchange: (e) => store.saveSettings({ apiKey: e.target.value.trim() }) }),
          'Held in this browser\'s localStorage and sent as a bearer token. Direct mode also needs API_SERVER_CORS_ORIGINS to name this origin.') : null,

        field('Profile',
          h('input', { type: 'text', value: s.profile, placeholder: '(none)',
            onchange: (e) => store.saveSettings({ profile: e.target.value.trim() }) }),
          'Only for gateways running gateway.multiplex_profiles; routes every call under /p/<profile>/.'),

        field('Status poll interval',
          h('input', { type: 'number', min: '1500', step: '500', value: s.pollMs,
            onchange: (e) => store.saveSettings({ pollMs: Number(e.target.value) || 4000 }) }),
          'Backstop for the run event stream, in milliseconds.'),

        h('div', { style: { display: 'flex', gap: '6px' } },
          h('button.primary', { onclick: probe }, 'Test the line'),
          h('button.ghost', { onclick: () => store.loadCatalog().then(() => toast('catalog reloaded', 'ok')) }, 'Reload catalog')),
      ];
      wiring.append(...rows.filter(Boolean));
    }

    /* ── line status ────────────────────────────────────────── */
    function paintStatus() {
      clear(status);
      const c = store.connection;

      status.append(h('div.jack',
        lamp(c.state === 'up' ? 'on' : c.state === 'down' ? 'failed' : 'off'),
        h('span.j-name', 'LINE'),
        h('span.j-val', c.state === 'up' ? 'ANSWERING' : c.state === 'down' ? 'NO ANSWER' : 'UNTESTED')));

      if (c.detail) status.append(h('p.hot.mono-wrap', { style: { fontSize: '10px' } }, c.detail));

      const health = c.health ?? {};
      const rows = [
        ['STATUS', health.status],
        ['VERSION', health.version],
        ['UPTIME', health.uptime_seconds != null ? `${Math.round(health.uptime_seconds / 60)} min` : null],
        ['MODEL', health.model ? `${health.model.id ?? '?'} · ${health.model.provider ?? '?'}` : null],
        ['ACTIVE RUNS', health.runs ? `${health.runs.active ?? 0} / ${health.runs.max_concurrent ?? '?'}` : null],
        ['PLATFORMS', health.gateway?.platforms?.join(', ')],
        ['MEMORY', health.memory ? `${health.memory.sessions ?? '?'} sessions · ${health.memory.backend ?? '?'}` : null],
        ['DRAINING', health.gateway?.draining === true ? 'YES' : health.gateway ? 'no' : null],
      ].filter(([, v]) => v != null && v !== '');

      for (const [k, v] of rows) {
        status.append(h('div.jack', h('span'), h('span.j-name', k), h('span.j-val', String(v))));
      }

      if (!rows.length && c.state !== 'down') {
        status.append(h('p.faint.tiny', 'GET /health/detailed returned no fields this build recognises.'));
      }
    }

    /* ── advertised capabilities ────────────────────────────── */
    function paintCaps() {
      clear(caps);
      const cap = store.connection.capabilities;
      if (!cap) { caps.append(h('p.faint', 'no answer from GET /v1/capabilities.')); return; }

      const chips = h('div.certs');
      for (const [k, v] of Object.entries(cap)) {
        if (v && typeof v === 'object') {
          chips.append(h('span.cert', { 'data-on': v.enabled === false ? '0' : '1', title: JSON.stringify(v) }, k));
        } else {
          chips.append(h('span.cert', { 'data-on': v ? '1' : '0' }, k));
        }
      }
      caps.append(chips);

      const d = cap.delegation;
      if (d && typeof d === 'object') {
        caps.append(h('hr.rule'), h('div.tiny.dim', 'DELEGATION'),
          h('div.tiny', `max concurrent children: ${d.max_concurrent_children ?? '?'} · max spawn depth: ${d.max_spawn_depth ?? '?'}`));
      }
    }

    /* ── console ────────────────────────────────────────────── */
    function paintLog() {
      clear(logBox);
      for (const e of store.console.slice(-120)) {
        logBox.append(h('div.log-line', { class: e.kind === 'error' ? 'hot' : '' },
          h('span.ts', `${new Date(e.at).toLocaleTimeString()}  `), e.message));
      }
      logBox.scrollTop = logBox.scrollHeight;
    }

    async function probe() {
      await store.probe();
      if (store.connection.state === 'up') {
        toast('the line is open', 'ok');
        store.loadCatalog().catch(() => {});
      } else {
        toast('no answer — check the gateway', 'error');
      }
    }

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Switchboard'),
        h('span.sub', 'wiring · line status · advertised capabilities')),
      h('div.cols.two',
        panel('Wiring', wiring),
        h('div', { style: { display: 'grid', gap: '12px', alignContent: 'start' } },
          panel('Line status', status),
          panel('What this gateway says it can do', caps))),
      panel('Console', logBox),
      panel('Bringing a gateway up', bootInstructions())));

    paintWiring();
    paintStatus();
    paintCaps();
    paintLog();

    const offs = [
      store.on('connection', () => { paintStatus(); paintCaps(); }),
      store.on('console', paintLog),
      store.on('settings', () => {}),
    ];
    return () => offs.forEach((f) => f());
  },
};

function transportNote(t) {
  return {
    proxy: 'server.py forwards /hermes/* to the gateway and adds the bearer token server-side. No secret reaches this page.',
    direct: 'This page calls the gateway itself. Needs the key in the browser and the gateway\'s CORS allowlist.',
    simulation: 'No network at all. Fabricated runs, sessions and jobs in the exact shapes the gateway emits.',
  }[t];
}

function bootInstructions() {
  const pre = (s) => h('pre.mono-wrap', { style: { margin: '4px 0', color: 'var(--ink-hi)' } }, s);
  return h('div', { style: { fontSize: '11px' } },
    h('p.dim', '1 — enable the API server in ~/.hermes/.env:'),
    pre('API_SERVER_ENABLED=true\nAPI_SERVER_KEY=change-me-local-dev'),
    h('p.dim', '2 — start the gateway:'),
    pre('hermes gateway'),
    h('p.dim', '3 — serve this dashboard with the key held server-side:'),
    pre('HERMES_API_KEY=change-me-local-dev \\\n  python3 hermes-desk/server.py'),
    h('p.dim', 'Then set the transport above to PROXY and test the line. For direct browser access instead, add this origin to API_SERVER_CORS_ORIGINS.'));
}
