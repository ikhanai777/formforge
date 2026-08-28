/* ═══════════════════════════════════════════════════════════════
   store.js — application state, persistence, and the run tracker.

   Hermes has no "list every run" endpoint: a run id is handed to
   whoever created it. So the dashboard keeps its own ledger of the
   runs it dispatched (in localStorage, so a reload does not lose the
   floor) and re-attaches to each one's event stream on boot.
   ═══════════════════════════════════════════════════════════════ */

import { HermesClient } from './api.js';
import { MockHermes } from './mock.js';
import { readRunEvent, TERMINAL_STATUSES, employeeName, badgeNumber, titleForTool } from './adapters.js';

const LS_SETTINGS = 'hermes-desk.settings.v1';
const LS_LEDGER = 'hermes-desk.ledger.v1';

const DEFAULT_SETTINGS = {
  transport: 'proxy',           // 'proxy' | 'direct' | 'simulation'
  baseUrl: '/hermes',
  apiKey: '',
  profile: '',
  model: '',
  provider: '',
  pollMs: 4000,
  theme: 'amber',
  sound: false,
  approvalRequired: false,
};

class Store {
  constructor() {
    this.settings = { ...DEFAULT_SETTINGS, ...loadJSON(LS_SETTINGS, {}) };
    this.runs = new Map();        // runId -> run record
    this.streams = new Map();     // runId -> {cancel}
    this.pollers = new Map();     // runId -> interval handle
    this.console = [];            // switchboard log
    this.connection = { state: 'unknown', detail: null, capabilities: null, health: null };
    this.catalog = { models: [], toolsets: [], skills: [] };
    this.listeners = new Map();

    for (const rec of loadJSON(LS_LEDGER, [])) this.runs.set(rec.id, reviveRun(rec));
    this.client = this.buildClient();
  }

  /* ── events ───────────────────────────────────────────────── */

  on(topic, fn) {
    if (!this.listeners.has(topic)) this.listeners.set(topic, new Set());
    this.listeners.get(topic).add(fn);
    return () => this.listeners.get(topic).delete(fn);
  }

  emit(topic, payload) {
    for (const fn of this.listeners.get(topic) ?? []) fn(payload);
    if (topic !== '*') for (const fn of this.listeners.get('*') ?? []) fn(topic, payload);
  }

  /* ── settings & transport ─────────────────────────────────── */

  buildClient() {
    if (this.settings.transport === 'simulation') return new MockHermes();
    return new HermesClient({
      baseUrl: this.settings.transport === 'direct' ? this.settings.baseUrl : '/hermes',
      apiKey: this.settings.transport === 'direct' ? this.settings.apiKey : '',
      profile: this.settings.profile,
    });
  }

  saveSettings(patch) {
    const before = this.settings.transport + this.settings.baseUrl + this.settings.apiKey + this.settings.profile;
    this.settings = { ...this.settings, ...patch };
    saveJSON(LS_SETTINGS, this.settings);
    const after = this.settings.transport + this.settings.baseUrl + this.settings.apiKey + this.settings.profile;
    if (before !== after) {
      this.detachAll();
      this.client = this.buildClient();
      this.emit('transport');
    }
    this.emit('settings');
  }

  get simulated() { return this.settings.transport === 'simulation'; }

  /* ── switchboard ──────────────────────────────────────────── */

  async probe() {
    try {
      const health = await this.client.healthDetailed().catch(() => this.client.health());
      const capabilities = await this.client.capabilities().catch(() => null);
      this.connection = { state: 'up', detail: null, health, capabilities };
      this.log('ok', 'switchboard answered');
    } catch (err) {
      this.connection = { state: 'down', detail: err.message, health: null, capabilities: null };
      this.log('error', `switchboard: ${err.message}`);
    }
    this.emit('connection');
    return this.connection;
  }

  async loadCatalog() {
    const [models, toolsets, skills] = await Promise.all([
      this.client.models().catch(() => null),
      this.client.toolsets().catch(() => null),
      this.client.skills().catch(() => null),
    ]);
    const A = await import('./adapters.js');
    this.catalog = {
      models: models ? A.readModels(models) : [],
      toolsets: toolsets ? A.readToolsets(toolsets) : [],
      skills: skills ? A.readSkills(skills) : [],
    };
    this.emit('catalog');
    return this.catalog;
  }

  /* ── the ledger ───────────────────────────────────────────── */

  get runList() {
    return [...this.runs.values()].sort((a, b) => b.createdAt - a.createdAt);
  }

  get activeRuns() {
    return this.runList.filter((r) => !TERMINAL_STATUSES.has(r.status));
  }

  /** Top-level runs, each with its subagents attached. */
  get teams() {
    const byParent = new Map();
    for (const r of this.runs.values()) {
      if (!r.parentId) continue;
      if (!byParent.has(r.parentId)) byParent.set(r.parentId, []);
      byParent.get(r.parentId).push(r);
    }
    return this.runList
      .filter((r) => !r.parentId)
      .map((lead) => ({ lead, crew: (byParent.get(lead.id) ?? []).sort((a, b) => a.createdAt - b.createdAt) }));
  }

  upsertRun(id, patch) {
    const existing = this.runs.get(id);
    const rec = existing ?? blankRun(id);
    Object.assign(rec, patch, { updatedAt: Date.now() });
    this.runs.set(id, rec);
    this.persistLedger();
    this.emit('runs');
    return rec;
  }

  appendRunLog(id, line) {
    const rec = this.runs.get(id);
    if (!rec) return;
    rec.log.push({ at: Date.now(), line });
    if (rec.log.length > 200) rec.log.splice(0, rec.log.length - 200);
    this.emit('runlog', id);
  }

  forgetRun(id) {
    this.detach(id);
    for (const r of this.runs.values()) if (r.parentId === id) this.runs.delete(r.id);
    this.runs.delete(id);
    this.persistLedger();
    this.emit('runs');
  }

  clearFiled() {
    for (const r of this.runList) {
      if (!r.parentId && TERMINAL_STATUSES.has(r.status)) this.forgetRun(r.id);
    }
  }

  persistLedger() {
    // Logs are transient; the ledger only needs enough to rebuild the floor.
    saveJSON(LS_LEDGER, this.runList.map(({ log, ...rest }) => rest));
  }

  /* ── dispatching and tracking work ────────────────────────── */

  async dispatch({ prompt, model, provider, context, approvalRequired, sessionId }) {
    const order = { prompt };
    if (model || this.settings.model) order.model = model || this.settings.model;
    if (provider || this.settings.provider) order.provider = provider || this.settings.provider;
    if (context && Object.keys(context).length) order.context = context;
    if (approvalRequired ?? this.settings.approvalRequired) order.approval_required = true;
    if (sessionId) order.session_id = sessionId;

    const res = await this.client.createRun(order);
    const id = res?.run_id ?? res?.id;
    if (!id) throw new Error('gateway accepted the order but returned no run_id');

    this.upsertRun(id, {
      id,
      prompt,
      status: res.status ?? 'queued',
      createdAt: Date.now(),
      model: order.model ?? null,
    });
    this.log('ok', `work order ${id} dispatched`);
    this.attach(id);
    return id;
  }

  /** Subscribe to a run's event stream and poll its status as a backstop. */
  attach(runId) {
    if (this.streams.has(runId)) return;

    const handle = this.client.runEvents(runId, {
      onEvent: (frame) => this.ingest(runId, frame),
      onError: (err) => this.log('error', `run ${short(runId)} stream: ${err.message}`),
    });
    this.streams.set(runId, handle);

    const tick = async () => {
      try {
        const s = await this.client.getRun(runId);
        const status = s?.status ?? 'running';
        this.upsertRun(runId, {
          status,
          result: s?.result ?? this.runs.get(runId)?.result ?? null,
          error: s?.error?.message ?? s?.error ?? this.runs.get(runId)?.error ?? null,
        });
        if (TERMINAL_STATUSES.has(status)) this.detach(runId);
      } catch (err) {
        // A subagent id is not always a pollable run; stop pestering it.
        if (err.status === 404) this.detachPoller(runId);
      }
    };
    tick();
    this.pollers.set(runId, setInterval(tick, Math.max(1500, this.settings.pollMs)));
  }

  /** Fold one event frame into the run record and its crew. */
  ingest(runId, frame) {
    const e = readRunEvent(frame);
    const patch = { status: statusFromEvent(e.type) ?? this.runs.get(runId)?.status ?? 'running' };

    if (e.tool) { patch.tool = e.tool; patch.title = titleForTool(e.tool); }
    if (e.note) patch.note = e.note;
    if (e.iteration != null) patch.iteration = e.iteration;
    if (e.maxIterations != null) patch.maxIterations = e.maxIterations;
    if (e.result) patch.result = e.result;
    if (e.error) patch.error = e.error;

    this.upsertRun(runId, patch);
    this.appendRunLog(runId, e.note ? `${e.type} · ${e.tool ?? ''} ${e.note}`.trim() : e.type);

    // delegate_task handed back subagent ids — put them on the floor.
    for (const child of e.children) {
      if (this.runs.has(child.id)) continue;
      this.upsertRun(child.id, {
        id: child.id,
        parentId: runId,
        prompt: child.goal ?? '(delegated task)',
        status: 'running',
        createdAt: Date.now(),
      });
      this.log('ok', `subagent ${short(child.id)} reporting to ${short(runId)}`);
      this.attach(child.id);
    }

    if (TERMINAL_STATUSES.has(patch.status)) {
      for (const r of this.runs.values()) {
        if (r.parentId === runId && !TERMINAL_STATUSES.has(r.status)) {
          this.upsertRun(r.id, { status: 'completed' });
        }
      }
      this.detach(runId);
      this.emit('run-finished', runId);
    }
  }

  detachPoller(runId) {
    const p = this.pollers.get(runId);
    if (p) { clearInterval(p); this.pollers.delete(runId); }
  }

  detach(runId) {
    this.detachPoller(runId);
    const s = this.streams.get(runId);
    if (s) { s.cancel(); this.streams.delete(runId); }
  }

  detachAll() {
    for (const id of [...this.streams.keys()]) this.detach(id);
    for (const id of [...this.pollers.keys()]) this.detachPoller(id);
  }

  /** After a reload, pick the floor back up where it was left. */
  resumeAll() {
    for (const r of this.activeRuns) this.attach(r.id);
  }

  async stop(runId)             { await this.client.stopRun(runId); this.upsertRun(runId, { status: 'stopped' }); this.detach(runId); }
  async steer(runId, guidance)  { await this.client.steerRun(runId, guidance); this.appendRunLog(runId, `↯ steered: ${guidance}`); }
  async approve(runId, ok, note){ await this.client.approveRun(runId, ok, note); this.upsertRun(runId, { status: ok ? 'running' : 'stopped' }); }

  /* ── console log ──────────────────────────────────────────── */

  log(kind, message) {
    this.console.push({ at: Date.now(), kind, message });
    if (this.console.length > 300) this.console.shift();
    this.emit('console');
  }
}

/* ── helpers ────────────────────────────────────────────────── */

function blankRun(id) {
  return {
    id,
    parentId: null,
    prompt: '',
    status: 'queued',
    tool: null,
    title: 'GENERAL CLERK',
    note: null,
    iteration: null,
    maxIterations: null,
    result: null,
    error: null,
    model: null,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    log: [],
    name: employeeName(id),
    badge: badgeNumber(id),
  };
}

function reviveRun(rec) { return { ...blankRun(rec.id), ...rec, log: [] }; }

function statusFromEvent(type) {
  if (type === 'run.completed') return 'completed';
  if (type === 'run.failed') return 'failed';
  if (type === 'run.started' || type === 'run.progress') return 'running';
  if (type?.includes('approval')) return 'waiting_for_approval';
  return null;
}

export function short(id, n = 8) {
  const s = String(id ?? '');
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function loadJSON(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
  catch { return fallback; }
}

function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}

export const store = new Store();
