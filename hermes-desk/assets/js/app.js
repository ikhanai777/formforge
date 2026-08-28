/* ═══════════════════════════════════════════════════════════════
   app.js — boot, department switching, and the chrome that is
   always on screen: letterhead, clock, status line.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, fmtClock, toast, beep } from './ui.js';
import { markSVG } from './sprites.js';
import { store } from './store.js';

import { floor } from './views/floor.js';
import { orders } from './views/orders.js';
import { dispatch } from './views/dispatch.js';
import { personnel } from './views/personnel.js';
import { archives } from './views/archives.js';
import { nightshift } from './views/nightshift.js';
import { switchboard } from './views/switchboard.js';

const DEPARTMENTS = [floor, orders, dispatch, personnel, archives, nightshift, switchboard];
const THEMES = ['amber', 'green', 'ibm'];

const surface = document.getElementById('surface');
const tabbar = document.getElementById('tabs');

let current = null;
let teardown = null;

/* ── routing ──────────────────────────────────────────────────── */

function go(id) {
  const dept = DEPARTMENTS.find((d) => d.id === id) ?? DEPARTMENTS[0];
  if (current === dept.id) return;

  teardown?.();
  teardown = null;
  current = dept.id;

  clear(surface);
  surface.scrollTop = 0;
  teardown = dept.render(surface) ?? null;

  paintTabs();
  location.hash = `#${dept.id}`;
  if (store.settings.sound) beep('tick');
}

function paintTabs() {
  clear(tabbar);
  for (const d of DEPARTMENTS) {
    const count = d.badge?.();
    tabbar.append(h('button.tab', {
      role: 'tab',
      'aria-selected': String(current === d.id),
      onclick: () => go(d.id),
    },
      h('span.fkey', d.fkey),
      d.label.toUpperCase(),
      count ? h('span.badge', String(count)) : null));
  }
}

/* ── always-on chrome ─────────────────────────────────────────── */

function paintStatusLine() {
  const s = store.settings;
  const lampEl = document.getElementById('conn-lamp');
  const text = document.getElementById('conn-text');
  const stamp = document.getElementById('mode-stamp');

  const sim = store.simulated;
  const up = store.connection.state === 'up';

  lampEl.dataset.state = sim ? 'queued' : up ? 'on' : store.connection.state === 'down' ? 'failed' : 'off';
  text.textContent = sim ? 'SIMULATION' : up ? `CONNECTED · ${s.transport.toUpperCase()}` : 'NO GATEWAY';

  stamp.textContent = sim ? 'SIMULATION' : up ? 'LIVE WIRE' : 'LINE DOWN';
  stamp.dataset.live = !sim && up ? '1' : '0';

  const active = store.activeRuns;
  document.getElementById('sl-floor').textContent =
    `FLOOR ${active.filter((r) => r.status === 'running').length}/${store.runs.size}`;
  document.getElementById('sl-queue').textContent =
    `QUEUE ${active.filter((r) => r.status === 'queued').length}`;

  paintTabs();
}

function setTheme(name) {
  document.documentElement.dataset.theme = name;
  document.getElementById('theme-toggle').textContent = name.toUpperCase();
  store.saveSettings({ theme: name });
}

/* ── boot ─────────────────────────────────────────────────────── */

function boot() {
  document.getElementById('lh-mark').innerHTML = markSVG();
  setTheme(store.settings.theme ?? 'amber');

  const soundBtn = document.getElementById('sound-toggle');
  const paintSound = () => { soundBtn.textContent = store.settings.sound ? '♪ ON' : '♪ OFF'; };
  paintSound();
  soundBtn.onclick = () => {
    store.saveSettings({ sound: !store.settings.sound });
    paintSound();
    if (store.settings.sound) beep('ok');
  };

  document.getElementById('theme-toggle').onclick = () => {
    const next = THEMES[(THEMES.indexOf(document.documentElement.dataset.theme) + 1) % THEMES.length];
    setTheme(next);
  };

  setInterval(() => { document.getElementById('clock').textContent = fmtClock(); }, 1000);
  document.getElementById('clock').textContent = fmtClock();

  // Any view can ask to switch departments.
  window.addEventListener('goto', (e) => go(e.detail));

  // F1–F7 pick a department, the way the terminal it is pretending
  // to be would have. Ignored while typing.
  window.addEventListener('keydown', (e) => {
    const m = /^F([1-7])$/.exec(e.key);
    if (!m) return;
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    e.preventDefault();
    go(DEPARTMENTS[Number(m[1]) - 1].id);
  });

  store.on('connection', paintStatusLine);
  store.on('settings', paintStatusLine);
  store.on('transport', () => { paintStatusLine(); store.probe(); });
  store.on('runs', paintStatusLine);

  store.on('run-finished', (id) => {
    const run = store.runs.get(id);
    if (!run || run.parentId) return;
    toast(`work order ${id.slice(0, 10)} filed`, 'ok');
    if (store.settings.sound) beep(run.status === 'failed' ? 'error' : 'ok');
  });

  go((location.hash || '#floor').slice(1));
  paintStatusLine();

  // First contact: try the configured transport, and fall back to the
  // simulation so the floor is never a blank screen on a fresh clone.
  store.probe().then(() => {
    if (store.connection.state === 'up') {
      store.loadCatalog().catch(() => {});
      store.resumeAll();
      return;
    }
    if (store.settings.transport !== 'simulation') {
      store.log('warn', 'no gateway answered — falling back to SIMULATION');
      store.saveSettings({ transport: 'simulation' });
      store.probe().then(() => {
        store.loadCatalog().catch(() => {});
        toast('no gateway found — running in simulation. Wire it up in SWITCHBOARD (F7).', 'info', 8000);
      });
    } else {
      store.loadCatalog().catch(() => {});
    }
  });

  window.addEventListener('beforeunload', () => store.detachAll());
}

boot();
