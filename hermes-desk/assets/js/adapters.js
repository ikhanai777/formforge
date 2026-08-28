/* ═══════════════════════════════════════════════════════════════
   adapters.js — the translation layer between Hermes payloads and
   the agency metaphor the interface renders.

   Hermes' documented shapes are honoured exactly where they are
   documented (run status values, event `type` names, the OpenAI
   error/choice envelopes). Where a payload is only loosely specified
   — the `data` blob inside a `run.progress` event, which carries
   whatever the executing tool reported — the readers below are
   deliberately permissive: they probe a list of plausible keys and
   fall back to something displayable rather than throwing.

   Adapting to a Hermes revision means editing this file and nothing
   else. Each reader states the keys it probes.
   ═══════════════════════════════════════════════════════════════ */

/* ── run status ─────────────────────────────────────────────────
   Documented set: queued | running | waiting_for_approval |
                   completed | failed
   `stopped`/`cancelled` are accepted because /v1/runs/{id}/stop
   exists and gateways differ on what they report afterwards.       */

export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'stopped', 'cancelled']);
export const ACTIVE_STATUSES = new Set(['queued', 'running', 'waiting_for_approval']);

export function statusLabel(status) {
  return ({
    queued: 'CLOCKED IN',
    running: 'AT WORK',
    waiting_for_approval: 'AWAITING SIGN-OFF',
    completed: 'FILED',
    failed: 'ERRORED',
    stopped: 'STOOD DOWN',
    cancelled: 'STOOD DOWN',
  })[status] || String(status || 'UNKNOWN').toUpperCase();
}

/* ── run lifecycle events ───────────────────────────────────────
   GET /v1/runs/{id}/events emits:
     { "type": "run.started|run.progress|run.completed|run.failed",
       "run_id": "...", "data": { ... } }
   Some gateways also put the type in the SSE `event:` field, so we
   read both.                                                      */

/**
 * @param {{event: string, data: any}} frame  from api.js parseSSEFrame
 * @returns {{
 *   type: string, runId: ?string, tool: ?string, note: ?string,
 *   iteration: ?number, maxIterations: ?number,
 *   children: Array<{id: string, goal: ?string}>,
 *   result: ?string, error: ?string, raw: any
 * }}
 */
export function readRunEvent(frame) {
  const d = frame?.data ?? {};
  const type = d.type || frame?.event || 'run.progress';
  const payload = d.data ?? d;

  return {
    type,
    runId: d.run_id ?? payload.run_id ?? null,
    tool: readToolName(payload),
    note: readNote(payload),
    iteration: firstNumber(payload, ['iteration', 'step', 'turn', 'current_iteration']),
    maxIterations: firstNumber(payload, ['max_iterations', 'iteration_limit', 'total_steps']),
    children: readChildren(payload),
    result: firstString(payload, ['result', 'output', 'summary', 'final']),
    error: readError(d) ?? readError(payload),
    raw: d,
  };
}

/** Tool-progress payloads name the running tool under one of these. */
function readToolName(p) {
  return firstString(p, ['tool', 'tool_name', 'name', 'function', 'action']);
}

/** A short human line about what is happening right now. */
function readNote(p) {
  const s = firstString(p, ['message', 'note', 'status', 'detail', 'description', 'text', 'progress']);
  if (s) return s;
  const args = p?.arguments ?? p?.args ?? p?.input;
  if (typeof args === 'string') return args;
  if (args && typeof args === 'object') {
    const q = firstString(args, ['query', 'goal', 'prompt', 'path', 'url', 'command']);
    if (q) return q;
  }
  return null;
}

/**
 * delegate_task returns a handle carrying `subagent_ids` and
 * `live_transcripts`. When that handle surfaces on the event stream we
 * turn each id into a desk on the floor reporting to this run.
 */
function readChildren(p) {
  const out = [];
  const ids = p?.subagent_ids ?? p?.children ?? p?.child_ids ?? p?.subagents;
  const tasks = Array.isArray(p?.tasks) ? p.tasks : [];

  if (Array.isArray(ids)) {
    ids.forEach((entry, i) => {
      if (typeof entry === 'string') {
        out.push({ id: entry, goal: tasks[i]?.goal ?? p?.goal ?? null });
      } else if (entry && typeof entry === 'object') {
        const id = firstString(entry, ['id', 'subagent_id', 'run_id', 'child_id']);
        if (id) out.push({ id, goal: firstString(entry, ['goal', 'task', 'objective']) });
      }
    });
  }
  return out;
}

function readError(p) {
  if (!p) return null;
  const e = p.error;
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object') return firstString(e, ['message', 'detail', 'code']) || 'unspecified failure';
  return null;
}

/* ── chat / completion streams ──────────────────────────────────
   Session chat returns chat-completion chunks:
     { choices: [{ delta: { content: "..." } }] }
   plus Hermes' own `hermes.tool.progress` frames. Responses-API
   streams use `response.delta` with a `delta` string.               */

/**
 * @returns {{kind: 'delta'|'tool'|'done'|'error'|'noise', text: string}}
 */
export function readChatEvent(frame) {
  if (!frame) return { kind: 'noise', text: '' };
  if (frame.event === 'done' || frame.raw === '[DONE]') return { kind: 'done', text: '' };

  const d = frame.data;
  if (!d) return { kind: 'noise', text: '' };

  const type = d.type || frame.event || '';

  if (type.includes('tool') || d.tool || d.tool_name) {
    const tool = readToolName(d) || readToolName(d.data ?? {}) || 'tool';
    const note = readNote(d) || readNote(d.data ?? {}) || '';
    return { kind: 'tool', text: note ? `${tool} — ${note}` : tool, tool };
  }

  if (type === 'response.done' || type === 'run.completed') return { kind: 'done', text: '' };

  const err = readError(d);
  if (err) return { kind: 'error', text: err };

  const delta =
    d.choices?.[0]?.delta?.content ??
    d.choices?.[0]?.message?.content ??
    (typeof d.delta === 'string' ? d.delta : d.delta?.content) ??
    d.output_text ??
    d.content;

  if (typeof delta === 'string' && delta) return { kind: 'delta', text: delta };
  if (Array.isArray(delta)) {
    const joined = delta.map((p) => (typeof p === 'string' ? p : p?.text ?? '')).join('');
    if (joined) return { kind: 'delta', text: joined };
  }
  return { kind: 'noise', text: '' };
}

/** Pull the assistant text out of a non-streaming turn. */
export function readChatResult(payload) {
  if (!payload) return '';
  return (
    payload.choices?.[0]?.message?.content ??
    payload.output ??
    payload.message ??
    payload.content ??
    payload.result ??
    ''
  );
}

/* ── collection shapes ──────────────────────────────────────────
   Hermes list endpoints variously return a bare array, an OpenAI
   `{object:"list", data:[...]}` envelope, or a named key.          */

export function readList(payload, ...keys) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== 'object') return [];
  for (const k of ['data', 'items', 'results', ...keys]) {
    if (Array.isArray(payload[k])) return payload[k];
  }
  const arrays = Object.values(payload).filter(Array.isArray);
  return arrays.length === 1 ? arrays[0] : [];
}

export function readModels(payload) {
  return readList(payload, 'models').map((m) => {
    if (typeof m === 'string') return { id: m, provider: null, owned_by: null };
    return {
      id: firstString(m, ['id', 'name', 'model']) || 'unnamed',
      provider: firstString(m, ['provider', 'owned_by', 'vendor']),
      context: firstNumber(m, ['context_length', 'context_window', 'max_tokens']),
      raw: m,
    };
  });
}

export function readToolsets(payload) {
  const list = readList(payload, 'toolsets');
  return list.map((t) => {
    if (typeof t === 'string') return { name: t, tools: [] };
    return {
      name: firstString(t, ['name', 'id', 'toolset']) || 'toolset',
      enabled: t.enabled !== false,
      description: firstString(t, ['description', 'summary']),
      tools: (readList(t, 'tools') || []).map((x) =>
        typeof x === 'string' ? x : firstString(x, ['name', 'id', 'tool']) || '?'),
    };
  });
}

export function readSkills(payload) {
  return readList(payload, 'skills').map((s) => {
    if (typeof s === 'string') return { name: s, description: '' };
    return {
      name: firstString(s, ['name', 'id', 'skill']) || 'skill',
      description: firstString(s, ['description', 'summary', 'about']) || '',
      source: firstString(s, ['source', 'origin', 'path']),
      uses: firstNumber(s, ['uses', 'invocations', 'count']),
    };
  });
}

export function readSessions(payload) {
  return readList(payload, 'sessions').map((s) => ({
    id: firstString(s, ['id', 'session_id', 'sid']) || '?',
    title: firstString(s, ['title', 'name', 'summary', 'preview']) || '(untitled)',
    created: firstTime(s, ['created_at', 'created', 'started_at']),
    updated: firstTime(s, ['updated_at', 'updated', 'last_message_at']),
    messages: firstNumber(s, ['message_count', 'messages', 'turns']),
    raw: s,
  }));
}

export function readJobs(payload) {
  return readList(payload, 'jobs', 'cronjobs').map((j) => ({
    id: firstString(j, ['id', 'job_id', 'name']) || '?',
    name: firstString(j, ['name', 'title', 'label']) || firstString(j, ['id', 'job_id']) || '(unnamed)',
    schedule: firstString(j, ['schedule', 'cron', 'cron_expression', 'when']) || '—',
    prompt: firstString(j, ['prompt', 'task', 'command', 'message']) || '',
    enabled: j?.enabled ?? j?.active ?? (j?.status !== 'paused'),
    lastRun: firstTime(j, ['last_run', 'last_run_at', 'last_fired_at']),
    nextRun: firstTime(j, ['next_run', 'next_run_at', 'next_fire_at']),
    raw: j,
  }));
}

export function readMessages(payload) {
  return readList(payload, 'messages').map((m) => ({
    role: firstString(m, ['role', 'author', 'sender']) || 'assistant',
    content: normalizeContent(m?.content ?? m?.text ?? m?.message ?? ''),
    at: firstTime(m, ['created_at', 'timestamp', 'at']),
  }));
}

/** Hermes accepts multimodal content arrays; flatten to text for display. */
function normalizeContent(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c.map((p) => {
      if (typeof p === 'string') return p;
      if (p?.type?.includes('image')) return '[image]';
      return p?.text ?? '';
    }).join('');
  }
  return c == null ? '' : String(c);
}

/* ── the agency metaphor ────────────────────────────────────────
   Tool names map to job titles; run ids map to a stable employee
   identity so the same worker keeps the same name and face for the
   life of the run.                                                 */

const TITLE_RULES = [
  [/delegate|orchestrat|spawn|subagent/i, 'FLOOR SUPERVISOR'],
  [/search|browse|fetch|extract|crawl|web/i, 'FIELD RESEARCH'],
  [/execute_code|python|repl|compute|sandbox/i, 'COMPUTATION'],
  [/shell|bash|terminal|exec|process/i, 'OPERATIONS'],
  [/write|edit|patch|apply|create_file|draft/i, 'DRAFTING'],
  [/read|open|view|list|glob|grep|inspect/i, 'RECORDS CLERK'],
  [/memory|remember|recall|session|history|archive/i, 'ARCHIVIST'],
  [/image|vision|tts|speech|audio|render/i, 'MEDIA DEPT.'],
  [/send_message|telegram|discord|slack|email|notify/i, 'CORRESPONDENCE'],
  [/cron|schedule|job|timer/i, 'NIGHT SHIFT'],
  [/skill|learn|improve/i, 'TRAINING'],
  [/clarify|ask|approval/i, 'LIAISON'],
];

/** Job title for whatever the worker is currently holding. */
export function titleForTool(tool) {
  if (!tool) return 'GENERAL CLERK';
  for (const [re, title] of TITLE_RULES) if (re.test(tool)) return title;
  return 'GENERAL CLERK';
}

const FIRST = [
  'MARGERY', 'DELROY', 'HORACE', 'PRUDENCE', 'CLIFFORD', 'ETHEL', 'RANDALL',
  'BEATRICE', 'MORTON', 'VIVIAN', 'ARCHIE', 'DOLORES', 'GILBERT', 'HARRIET',
  'NORBERT', 'CONSTANCE', 'RUFUS', 'AGNES', 'LEONARD', 'MYRTLE', 'CECIL', 'IRENE',
];

const LAST = [
  'PENHALIGON', 'BRACKWATER', 'FINCH', 'OSTRANDER', 'QUILL', 'HOLLOWAY', 'DRUMMOND',
  'VANCE', 'MERRIWEATHER', 'STOKES', 'ABERNATHY', 'CROWE', 'LOCKHART', 'WHITTAKER',
  'REDGRAVE', 'PARSLOW', 'DUNNAGE', 'HEATHCOTE', 'BLYTHE', 'CARRICK',
];

/** Stable 32-bit hash so an id always yields the same employee. */
export function hashId(id) {
  let h = 2166136261;
  const s = String(id ?? '');
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function employeeName(id) {
  const h = hashId(id);
  return `${FIRST[h % FIRST.length]} ${LAST[(h >>> 8) % LAST.length]}`;
}

/** Two-digit-ish badge number, stable per id. */
export function badgeNumber(id) {
  return String(hashId(id) % 9000 + 1000);
}

/* ── small readers ──────────────────────────────────────────── */

function firstString(obj, keys) {
  if (!obj || typeof obj !== 'object') return null;
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  return null;
}

function firstNumber(obj, keys) {
  if (!obj || typeof obj !== 'object') return null;
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return null;
}

function firstTime(obj, keys) {
  if (!obj || typeof obj !== 'object') return null;
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'number') return v > 1e11 ? v : v * 1000; // s or ms
    if (typeof v === 'string') {
      const t = Date.parse(v);
      if (!Number.isNaN(t)) return t;
    }
  }
  return null;
}
