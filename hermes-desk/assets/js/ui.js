/* ═══════════════════════════════════════════════════════════════
   ui.js — DOM helpers and the shared retro widgets.
   ═══════════════════════════════════════════════════════════════ */

/**
 * Terse element builder.
 *   h('div.desk', { onclick }, 'text', child)
 * The tag accepts `tag.class.class#id`.
 */
export function h(spec, props = null, ...children) {
  const [head, ...classes] = String(spec).split('.');
  const [tag, id] = head.split('#');
  const node = document.createElement(tag || 'div');
  if (id) node.id = id;
  if (classes.length) node.className = classes.join(' ');

  // Anything that is not a plain options object — a string, a number,
  // a node, an array — is a child that happened to land in second place.
  if (props != null && (typeof props !== 'object' || props.nodeType || Array.isArray(props))) {
    children.unshift(props);
    props = null;
  }

  for (const [k, v] of Object.entries(props ?? {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = [node.className, v].filter(Boolean).join(' ');
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k in node && k !== 'list' && typeof v !== 'boolean') node[k] = v;
    else node.setAttribute(k, v === true ? '' : v);
  }

  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children.flat(4)) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/** A titled panel with an optional right-hand action row. */
export function panel(title, body, actions = null) {
  return h('section.panel',
    h('header.panel-head', title, h('span.ph-rule'), actions),
    h('div.panel-body', body));
}

export function lamp(state) { return h('span.lamp', { 'data-state': state ?? 'off' }); }

export function field(label, control, hint) {
  return h('label.field', h('span.lbl', label), control, hint ? h('span.hint', hint) : null);
}

export function select(options, value, onchange) {
  const s = h('select', { onchange: (e) => onchange(e.target.value) });
  for (const o of options) {
    const [val, text] = Array.isArray(o) ? o : [o, o];
    s.append(h('option', { value: val, selected: String(val) === String(value) }, text));
  }
  return s;
}

export function table(headers, rows, emptyText = 'nothing on file') {
  const tbody = h('tbody');
  if (!rows.length) {
    tbody.append(h('tr.empty-row', h('td', { colspan: headers.length }, emptyText)));
  } else {
    for (const r of rows) tbody.append(r);
  }
  return h('table.tbl',
    h('thead', h('tr', headers.map((x) => h('th', x)))),
    tbody);
}

/* ── toasts ─────────────────────────────────────────────────── */

export function toast(message, kind = 'info', ms = 4200) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const t = h('div.toast', { 'data-kind': kind }, message);
  host.append(t);
  setTimeout(() => t.remove(), ms);
}

/* ── modal ──────────────────────────────────────────────────── */

export function modal({ title, body, actions = [], onClose }) {
  const close = () => { scrim.remove(); document.removeEventListener('keydown', onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };

  const scrim = h('div.modal-scrim', { onclick: (e) => { if (e.target === scrim) close(); } },
    h('div.modal',
      h('header.modal-head', title, h('span.spacer'), h('button.ghost', { onclick: close }, '✕')),
      h('div.modal-body', body),
      h('footer.modal-foot',
        ...actions.map((a) => h('button', {
          class: a.kind ?? '',
          onclick: () => { a.onClick?.(close); if (a.closes !== false) close(); },
        }, a.label)),
        h('button.ghost', { onclick: close }, 'Close'))));

  document.body.append(scrim);
  document.addEventListener('keydown', onKey);
  return close;
}

export function confirmBox(message, onYes) {
  modal({
    title: 'Confirm',
    body: h('p.mono-wrap', message),
    actions: [{ label: 'Proceed', kind: 'primary', onClick: onYes }],
  });
}

/* ── formatting ─────────────────────────────────────────────── */

export function fmtClock(d = new Date()) {
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0')).join(':');
}

export function fmtStamp(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtAgo(ms) {
  if (!ms) return '—';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function fmtDuration(fromMs, toMs = Date.now()) {
  const s = Math.max(0, Math.round((toMs - fromMs) / 1000));
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

/* ── console tones ──────────────────────────────────────────── */

let audio = null;

/** A short square-wave blip. Off unless the operator turns it on. */
export function beep(kind = 'tick') {
  try {
    audio ??= new (window.AudioContext || window.webkitAudioContext)();
    const freq = { tick: 660, ok: 880, warn: 440, error: 180 }[kind] ?? 660;
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = 'square';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.035, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.09);
    osc.connect(gain).connect(audio.destination);
    osc.start();
    osc.stop(audio.currentTime + 0.1);
  } catch { /* no audio device, or blocked before a gesture */ }
}
