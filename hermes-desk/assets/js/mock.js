/* ═══════════════════════════════════════════════════════════════
   mock.js — the SIMULATION transport.

   Implements the same surface as HermesClient, entirely in memory,
   emitting the same payload shapes the real gateway emits. It exists
   so the floor can be looked at, styled and demoed before a gateway
   is reachable — and so the adapters in adapters.js are exercised by
   realistic frames rather than by hand-shaped ones.

   Nothing here talks to the network. The status bar reads
   "SIMULATION" in red whenever this transport is selected.
   ═══════════════════════════════════════════════════════════════ */

const TOOLS = [
  ['web_search', 'querying the trade directories'],
  ['web_extract', 'pulling the relevant column'],
  ['read_file', 'checking the ledger'],
  ['execute_code', 'running the figures'],
  ['write_file', 'typing up the memorandum'],
  ['shell', 'operating the machine'],
  ['memory', 'filing what was learned'],
  ['session_search', 'consulting last quarter'],
  ['image_generate', 'preparing the plate'],
  ['send_message', 'wiring the outcome'],
];

const SUBGOALS = [
  'survey the competing filings',
  'reconcile the two inventories',
  'draft the summary for review',
  'verify the arithmetic end to end',
  'collect the outstanding references',
  'test the proposed change',
];

const SKILLS = [
  ['weekly-digest', 'Compiles a Monday digest from the week of sessions.', 'self-authored'],
  ['repo-triage', 'Reads a repository and files an inventory of open work.', 'self-authored'],
  ['invoice-reader', 'Extracts totals and dates from scanned invoices.', 'agentskills.io'],
  ['inbox-sweep', 'Sorts correspondence and drafts replies for sign-off.', 'self-authored'],
  ['chart-writer', 'Turns a table into a labelled chart.', 'agentskills.io'],
];

const TOOLSETS = [
  { name: 'core', description: 'Always on.', tools: ['clarify', 'memory', 'session_search', 'todo'] },
  { name: 'files', description: 'Workspace access.', tools: ['read_file', 'write_file', 'edit_file', 'glob', 'grep'] },
  { name: 'web', description: 'Nous Portal web control.', tools: ['web_search', 'web_extract', 'web_browse', 'vision'] },
  { name: 'code', description: 'Programmatic tool calling.', tools: ['execute_code', 'shell'] },
  { name: 'delegation', description: 'Subagent spawning.', tools: ['delegate_task'] },
  { name: 'comms', description: 'Gateway platforms.', tools: ['send_message', 'cronjob'] },
  { name: 'media', description: 'Generation.', tools: ['image_generate', 'tts'] },
];

let seq = 0;
const nextId = (p) => `${p}_${(++seq).toString().padStart(3, '0')}${Math.random().toString(36).slice(2, 6)}`;
const pick = (a) => a[Math.floor(Math.random() * a.length)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class MockHermes {
  constructor() {
    this.isLive = false;
    this.runs = new Map();
    this.sessions = seedSessions();
    this.jobs = seedJobs();
    this.transcripts = new Map();
  }

  /* ── switchboard ────────────────────────────────────────── */

  async health() { return { status: 'ok' }; }

  async healthDetailed() {
    await sleep(120);
    return {
      status: 'ok',
      simulated: true,
      version: '0.7.0-sim',
      uptime_seconds: 4820,
      gateway: { platforms: ['api_server', 'telegram', 'discord'], draining: false },
      runs: { active: [...this.runs.values()].filter((r) => r.status === 'running').length, max_concurrent: 10 },
      model: { provider: 'nous-portal', id: 'Hermes-4-405B' },
      memory: { sessions: this.sessions.length, backend: 'sqlite+fts5' },
    };
  }

  async capabilities() {
    return {
      chat_completions: true,
      responses_api: true,
      run_submission: true,
      run_steering: true,
      run_approval: true,
      sessions_api: true,
      jobs_api: true,
      skills_discovery: true,
      toolsets_discovery: true,
      streaming: true,
      tool_progress: true,
      delegation: { enabled: true, max_concurrent_children: 3, max_spawn_depth: 1 },
    };
  }

  async models() {
    return { object: 'list', data: [
      { id: 'Hermes-4-405B', owned_by: 'nous-portal', context_length: 131072 },
      { id: 'Hermes-4-70B', owned_by: 'nous-portal', context_length: 131072 },
      { id: 'hermes-agent', owned_by: 'local-profile', context_length: 128000 },
      { id: 'claude-opus-5', owned_by: 'anthropic', context_length: 200000 },
      { id: 'gpt-4o', owned_by: 'openai', context_length: 128000 },
    ] };
  }

  async toolsets() { return { toolsets: TOOLSETS.map((t) => ({ ...t, enabled: t.name !== 'media' })) }; }

  async skills() {
    return { skills: SKILLS.map(([name, description, source], i) => ({
      name, description, source, uses: 3 + ((i * 7) % 19),
    })) };
  }

  /* ── runs ───────────────────────────────────────────────── */

  async createRun(order) {
    await sleep(180);
    const id = nextId('run');
    this.runs.set(id, {
      run_id: id,
      status: 'queued',
      prompt: order.prompt,
      model: order.model ?? 'Hermes-4-405B',
      approval_required: !!order.approval_required,
      result: null,
      error: null,
      created_at: Date.now(),
      updated_at: Date.now(),
      listeners: new Set(),
      step: 0,
      delegated: false,
    });
    return { run_id: id, status: 'queued' };
  }

  async getRun(runId) {
    const r = this.runs.get(runId);
    if (!r) { const e = new Error('no such run'); e.status = 404; throw e; }
    const { listeners, ...rest } = r;
    return rest;
  }

  async stopRun(runId) {
    const r = this.runs.get(runId);
    if (r) { r.status = 'stopped'; this.#emit(r, 'run.failed', { message: 'stood down by the floor' }); }
    return { ok: true };
  }

  async steerRun(runId, guidance) {
    const r = this.runs.get(runId);
    if (r) this.#emit(r, 'run.progress', { message: `noted: ${guidance}`, tool: 'clarify' });
    return { ok: true };
  }

  async approveRun(runId, approved) {
    const r = this.runs.get(runId);
    if (r) {
      r.status = approved ? 'running' : 'stopped';
      this.#emit(r, 'run.progress', { message: approved ? 'sign-off received' : 'sign-off refused' });
    }
    return { ok: true };
  }

  /**
   * Drives a plausible run: a few tool steps, sometimes a delegation
   * that spawns two subagents, then a filed result.
   */
  runEvents(runId, { onEvent } = {}) {
    const r = this.runs.get(runId);
    if (!r) return { done: Promise.resolve(), cancel() {} };

    r.listeners.add(onEvent);
    let stopped = false;

    const timer = setInterval(() => {
      if (stopped || r.status === 'stopped') return;

      if (r.step === 0) {
        r.status = 'running';
        this.#emit(r, 'run.started', { message: 'clocked in', max_iterations: 8 });
      } else if (r.approval_required && r.step === 2 && r.status === 'running') {
        r.status = 'waiting_for_approval';
        this.#emit(r, 'run.progress', { message: 'this one needs a signature', tool: 'clarify' });
        r.step++;
        return;
      } else if (r.status === 'waiting_for_approval') {
        return;
      } else if (!r.delegated && r.step === 3 && Math.random() < 0.75) {
        r.delegated = true;
        const kids = [nextId('sub'), nextId('sub')];
        for (const id of kids) {
          this.runs.set(id, {
            run_id: id, status: 'running', prompt: pick(SUBGOALS), model: r.model,
            result: null, error: null, created_at: Date.now(), updated_at: Date.now(),
            listeners: new Set(), step: 0, delegated: true, child: true,
          });
        }
        this.#emit(r, 'run.progress', {
          tool: 'delegate_task',
          message: 'split the work across the bullpen',
          subagent_ids: kids,
          tasks: kids.map(() => ({ goal: pick(SUBGOALS) })),
          live_transcripts: kids.map((k) => `~/.hermes/transcripts/${k}.log`),
        });
      } else if (r.step >= (r.child ? 6 : 9)) {
        r.status = 'completed';
        r.result = `Filed. ${r.prompt.slice(0, 60)}${r.prompt.length > 60 ? '…' : ''} — 1 memorandum, 2 attachments.`;
        this.#emit(r, 'run.completed', { result: r.result });
        clearInterval(timer);
      } else {
        const [tool, note] = pick(TOOLS);
        this.#emit(r, 'run.progress', {
          tool, message: note, iteration: r.step, max_iterations: r.child ? 6 : 9,
        });
      }
      r.step++;
      r.updated_at = Date.now();
    }, 1400 + Math.random() * 900);

    return {
      done: Promise.resolve(),
      cancel() { stopped = true; clearInterval(timer); r.listeners.delete(onEvent); },
    };
  }

  #emit(run, type, data) {
    const frame = { event: 'message', data: { type, run_id: run.run_id, data }, raw: '' };
    for (const fn of run.listeners) { try { fn(frame); } catch { /* listener gone */ } }
  }

  /* ── sessions ───────────────────────────────────────────── */

  async listSessions() { return { sessions: this.sessions }; }

  async createSession() {
    const s = { id: nextId('sess'), title: 'New correspondence', created_at: Date.now(), updated_at: Date.now(), message_count: 0 };
    this.sessions.unshift(s);
    this.transcripts.set(s.id, []);
    return s;
  }

  async getSession(id) { return this.sessions.find((s) => s.id === id) ?? null; }

  async sessionMessages(id) {
    return { messages: this.transcripts.get(id) ?? [
      { role: 'user', content: 'What did we agree last week?', created_at: Date.now() - 86400000 },
      { role: 'assistant', content: 'You asked for the digest on Mondays and the invoice sweep on the first of the month. Both are on the night shift.', created_at: Date.now() - 86300000 },
    ] };
  }

  async forkSession(id) {
    const src = this.sessions.find((s) => s.id === id);
    const s = { id: nextId('sess'), title: `${src?.title ?? 'session'} (branch)`, created_at: Date.now(), updated_at: Date.now(), message_count: src?.message_count ?? 0 };
    this.sessions.unshift(s);
    return s;
  }

  async deleteSession(id) {
    this.sessions = this.sessions.filter((s) => s.id !== id);
    return { ok: true };
  }

  async chat(sessionId, message) {
    await sleep(500);
    return { choices: [{ message: { role: 'assistant', content: this.#reply(message) } }] };
  }

  chatStream(sessionId, message, { onEvent } = {}) {
    let stopped = false;
    const done = (async () => {
      await sleep(320);
      if (stopped) return;
      const [tool, note] = pick(TOOLS);
      onEvent?.({ event: 'message', data: { type: 'hermes.tool.progress', tool, message: note }, raw: '' });

      const words = this.#reply(message).split(/(\s+)/);
      for (const w of words) {
        if (stopped) return;
        await sleep(22);
        onEvent?.({ event: 'message', data: { choices: [{ delta: { content: w } }] }, raw: '' });
      }
      onEvent?.({ event: 'done', data: null, raw: '[DONE]' });
    })();
    return { done, cancel() { stopped = true; } };
  }

  #reply(message) {
    const m = String(message).trim();
    return `Understood — "${m.slice(0, 90)}${m.length > 90 ? '…' : ''}".\n\n`
      + `This is the SIMULATION transport, so nothing was actually executed. Point the `
      + `switchboard at a running \`hermes gateway\` and this same panel will stream real `
      + `tokens and real tool progress from the agent.\n\n`
      + `Were this live, I would have opened a session, called the tools the toolsets panel `
      + `lists, and filed the result under Archives.`;
  }

  /* ── jobs ───────────────────────────────────────────────── */

  async listJobs() { return { jobs: this.jobs }; }

  async createJob(job) {
    const j = { id: nextId('job'), enabled: true, last_run: null,
      next_run: Date.now() + 3600000, ...job };
    this.jobs.unshift(j);
    return j;
  }

  async patchJob(id, patch) {
    const j = this.jobs.find((x) => x.id === id);
    if (j) Object.assign(j, patch);
    return j;
  }

  async deleteJob(id) { this.jobs = this.jobs.filter((j) => j.id !== id); return { ok: true }; }
  async pauseJob(id)  { return this.patchJob(id, { enabled: false }); }
  async resumeJob(id) { return this.patchJob(id, { enabled: true }); }

  async runJob(id) {
    const j = this.jobs.find((x) => x.id === id);
    if (j) j.last_run = Date.now();
    return { ok: true };
  }
}

function seedSessions() {
  const now = Date.now();
  return [
    { id: 'sess_ledger', title: 'Quarterly ledger reconciliation', created_at: now - 5 * 864e5, updated_at: now - 3600e3, message_count: 42 },
    { id: 'sess_intake', title: 'Supplier intake — Brackwater & Sons', created_at: now - 3 * 864e5, updated_at: now - 7200e3, message_count: 18 },
    { id: 'sess_digest', title: 'Monday digest — week 34', created_at: now - 864e5, updated_at: now - 1800e3, message_count: 7 },
    { id: 'sess_floor', title: 'Floor scheduling experiment', created_at: now - 12 * 864e5, updated_at: now - 9 * 864e5, message_count: 63 },
  ];
}

function seedJobs() {
  const now = Date.now();
  return [
    { id: 'job_digest', name: 'Monday digest', schedule: '0 8 * * 1', prompt: 'Summarise the week of sessions and wire it to Telegram.', enabled: true, last_run: now - 3 * 864e5, next_run: now + 4 * 864e5 },
    { id: 'job_sweep', name: 'Invoice sweep', schedule: '0 6 1 * *', prompt: 'Read new invoices, extract totals, file the discrepancies.', enabled: true, last_run: now - 20 * 864e5, next_run: now + 9 * 864e5 },
    { id: 'job_backup', name: 'Nightly memory compaction', schedule: '30 2 * * *', prompt: 'Compact long-term memory and report anything contradictory.', enabled: false, last_run: now - 864e5, next_run: null },
  ];
}
