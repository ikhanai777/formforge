/* ═══════════════════════════════════════════════════════════════
   DISPATCH — the front desk. A streaming conversation with the agent
   over POST /api/sessions/{id}/chat/stream, with tool progress shown
   inline as it arrives.
   ═══════════════════════════════════════════════════════════════ */

import { h, clear, panel, toast, select } from '../ui.js';
import { readChatEvent, readChatResult, readSessions, readMessages } from '../adapters.js';
import { store, short } from '../store.js';

const LS_SESSION = 'hermes-desk.dispatch-session';

export const dispatch = {
  id: 'dispatch',
  label: 'Dispatch',
  fkey: 'F3',

  render(host) {
    let sessionId = localStorage.getItem(LS_SESSION) || null;
    let live = null;

    const transcript = h('div.transcript');
    const input = h('textarea', {
      rows: 3,
      placeholder: 'Speak to the agent.  ⏎ sends · shift+⏎ new line',
      onkeydown: (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      },
    });

    const sendBtn = h('button.primary', { onclick: () => send() }, 'Send');
    const stopBtn = h('button.danger', { disabled: true, onclick: () => live?.cancel() }, 'Halt');
    const sessionPicker = h('div', { style: { minWidth: '260px' } });

    /* ── transcript rendering ───────────────────────────────── */

    function line(role, who, text) {
      const body = h('div.body', text);
      const node = h('div.msg', { 'data-role': role }, h('div.who', who), body);
      transcript.append(node);
      transcript.scrollTop = transcript.scrollHeight;
      return body;
    }

    function banner() {
      clear(transcript);
      line('tool', 'switchboard',
        sessionId
          ? `Connected to session ${short(sessionId, 24)}. The agent keeps its memory of this thread between turns.`
          : 'No session selected. Sending will open one (POST /api/sessions) and hold the thread.');
    }

    /* ── sending a turn ─────────────────────────────────────── */

    async function ensureSession() {
      if (sessionId) return sessionId;
      const s = await store.client.createSession({ title: 'Dispatch desk' });
      sessionId = s?.id ?? s?.session_id;
      if (!sessionId) throw new Error('gateway did not return a session id');
      localStorage.setItem(LS_SESSION, sessionId);
      paintSessions();
      return sessionId;
    }

    async function send() {
      const text = input.value.trim();
      if (!text || live) return;
      input.value = '';
      line('user', 'you', text);

      let id;
      try { id = await ensureSession(); }
      catch (err) { line('error', 'switchboard', err.message); return; }

      sendBtn.disabled = true;
      stopBtn.disabled = false;

      // The reply bubble is created on the first token, so tool-progress
      // lines appear above it in the order the agent actually did them.
      let body = null;
      const bubble = () => (body ??= line('assistant', 'hermes', ''));
      let got = false;

      live = store.client.chatStream(id, text, {
        onEvent: (frame) => {
          const e = readChatEvent(frame);
          if (e.kind === 'delta') { got = true; bubble().textContent += e.text; }
          else if (e.kind === 'tool') line('tool', '⚙ tool', e.text);
          else if (e.kind === 'error') line('error', 'error', e.text);
          else if (e.kind === 'done') finish();
          transcript.scrollTop = transcript.scrollHeight;
        },
        onError: (err) => { line('error', 'stream', err.message); finish(); },
      });

      // Some gateways do not implement the streaming route; fall back.
      live.done.then(async () => {
        if (got || !live) return finish();
        try {
          const res = await store.client.chat(id, text);
          bubble().textContent = readChatResult(res) || '(empty reply)';
        } catch (err) {
          line('error', 'switchboard', err.message);
        }
        finish();
      });

      function finish() {
        if (!live) return;
        live = null;
        sendBtn.disabled = false;
        stopBtn.disabled = true;
        if (body && !body.textContent) body.textContent = '(no content returned)';
      }
    }

    /* ── session picker ─────────────────────────────────────── */

    async function paintSessions() {
      clear(sessionPicker);
      let list = [];
      try { list = readSessions(await store.client.listSessions({ limit: 30 })); }
      catch { /* the picker is optional; a new session always works */ }

      const opts = [['', '— new session —']].concat(list.map((s) => [s.id, `${s.title} · ${short(s.id, 10)}`]));
      sessionPicker.append(select(opts, sessionId ?? '', async (v) => {
        sessionId = v || null;
        if (sessionId) localStorage.setItem(LS_SESSION, sessionId);
        else localStorage.removeItem(LS_SESSION);
        banner();
        if (sessionId) await loadHistory();
      }));
    }

    async function loadHistory() {
      try {
        const msgs = readMessages(await store.client.sessionMessages(sessionId));
        for (const m of msgs.slice(-30)) {
          line(m.role === 'user' ? 'user' : 'assistant', m.role === 'user' ? 'you' : 'hermes', m.content);
        }
      } catch { line('tool', 'switchboard', 'no transcript available for this session.'); }
    }

    /* ── layout ─────────────────────────────────────────────── */

    host.append(h('div.view',
      h('div.view-head',
        h('h2', 'Dispatch'),
        h('span.sub', 'POST /api/sessions/{id}/chat/stream'),
        h('span.spacer'),
        sessionPicker,
        h('button.ghost', {
          onclick: async () => {
            sessionId = null;
            localStorage.removeItem(LS_SESSION);
            await paintSessions();
            banner();
          },
        }, 'New thread'),
        h('button.ghost', {
          onclick: async () => {
            if (!sessionId) return toast('nothing to branch yet', 'error');
            try {
              const s = await store.client.forkSession(sessionId);
              toast(`branched to ${short(s?.id ?? '?', 12)}`, 'ok');
              await paintSessions();
            } catch (e) { toast(e.message, 'error'); }
          },
        }, 'Branch')),
      panel('Correspondence',
        h('div.dispatch',
          transcript,
          h('div.composer', input, h('div', { style: { display: 'grid', gap: '4px' } }, sendBtn, stopBtn))))));

    banner();
    paintSessions();
    if (sessionId) loadHistory();

    return () => { live?.cancel(); };
  },
};
