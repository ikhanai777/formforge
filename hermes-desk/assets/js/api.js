/* ═══════════════════════════════════════════════════════════════
   HermesClient — a thin, complete binding to the Hermes Agent
   API server (`hermes gateway` with API_SERVER_ENABLED=true).

   Every network call the dashboard makes goes through this file.
   Nothing else in the app knows a URL. If the Hermes API moves,
   this is the only file that changes.

   Endpoints covered (Hermes API server, default :8642):

     GET    /health                       liveness (public)
     GET    /health/detailed              readiness, authenticated
     GET    /v1/capabilities              advertised feature set
     GET    /v1/models                    available models
     GET    /v1/toolsets                  resolved toolsets -> tool lists
     GET    /v1/skills                    agent skills
     POST   /v1/runs                      dispatch an async agent run
     GET    /v1/runs/{id}                 poll run status
     GET    /v1/runs/{id}/events          SSE lifecycle + tool progress
     POST   /v1/runs/{id}/steer           inject guidance mid-run
     POST   /v1/runs/{id}/approval        resolve a pending approval
     POST   /v1/runs/{id}/stop            interrupt
     GET    /api/sessions                 list sessions
     POST   /api/sessions                 create session
     GET    /api/sessions/{id}            metadata
     GET    /api/sessions/{id}/messages   transcript
     POST   /api/sessions/{id}/chat       synchronous turn
     POST   /api/sessions/{id}/chat/stream  SSE turn
     POST   /api/sessions/{id}/fork       branch a session
     GET    /api/jobs                     scheduled runs
     POST   /api/jobs                     create
     PATCH  /api/jobs/{id}                update
     DELETE /api/jobs/{id}                remove
     POST   /api/jobs/{id}/pause|resume|run

   Auth is `Authorization: Bearer <API_SERVER_KEY>`. When the app is
   served by server.py the key stays on the server and baseUrl is the
   proxy prefix, so `apiKey` here is empty and no secret reaches the
   browser.
   ═══════════════════════════════════════════════════════════════ */

export class HermesError extends Error {
  constructor(message, { status = 0, code = null, type = null } = {}) {
    super(message);
    this.name = 'HermesError';
    this.status = status;
    this.code = code;
    this.type = type;
  }
}

export class HermesClient {
  /**
   * @param {object}  opts
   * @param {string}  opts.baseUrl  proxy prefix ("/hermes") or origin
   * @param {string} [opts.apiKey]  only when talking to Hermes directly
   * @param {string} [opts.profile] gateway.multiplex_profiles name
   */
  constructor({ baseUrl = '/hermes', apiKey = '', profile = '' } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.apiKey = apiKey;
    this.profile = profile;
    this.isLive = true;
  }

  /** Multiplexed gateways route every path under /p/<profile>/. */
  url(path) {
    const prefix = this.profile ? `/p/${encodeURIComponent(this.profile)}` : '';
    return `${this.baseUrl}${prefix}${path}`;
  }

  headers(extra = {}) {
    const h = { Accept: 'application/json', ...extra };
    if (this.apiKey) h.Authorization = `Bearer ${this.apiKey}`;
    return h;
  }

  async request(path, { method = 'GET', body, signal, headers } = {}) {
    const init = { method, signal, headers: this.headers(headers) };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let res;
    try {
      res = await fetch(this.url(path), init);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new HermesError(
        `cannot reach ${this.url(path)} — is \`hermes gateway\` running?`,
        { code: 'unreachable' },
      );
    }

    const text = await res.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch { payload = { raw: text }; } }

    if (!res.ok) {
      // Hermes returns OpenAI-shaped errors: { error: {message,type,code} }
      const e = payload?.error ?? {};
      throw new HermesError(e.message || `HTTP ${res.status} ${res.statusText}`, {
        status: res.status,
        code: e.code ?? null,
        type: e.type ?? null,
      });
    }
    return payload;
  }

  /**
   * Consume a Server-Sent Events response. EventSource cannot carry an
   * Authorization header, so we read the body stream by hand.
   *
   * @returns {{done: Promise<void>, cancel: () => void}}
   */
  stream(path, { method = 'GET', body, onEvent, onError } = {}) {
    const ctrl = new AbortController();
    const init = {
      method,
      signal: ctrl.signal,
      headers: this.headers({ Accept: 'text/event-stream' }),
    };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    const done = (async () => {
      let res;
      try {
        res = await fetch(this.url(path), init);
      } catch (err) {
        if (err.name !== 'AbortError') onError?.(new HermesError(`stream failed: ${err.message}`));
        return;
      }
      if (!res.ok || !res.body) {
        onError?.(new HermesError(`stream rejected (HTTP ${res.status})`, { status: res.status }));
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      try {
        for (;;) {
          const { value, done: finished } = await reader.read();
          if (finished) break;
          buffer += decoder.decode(value, { stream: true });

          // Frames are separated by a blank line; \r\n\r\n is legal too.
          let split;
          while ((split = buffer.search(/\r?\n\r?\n/)) !== -1) {
            const frame = buffer.slice(0, split);
            buffer = buffer.slice(split).replace(/^\r?\n\r?\n/, '');
            const parsed = parseSSEFrame(frame);
            if (parsed) onEvent?.(parsed);
          }
        }
      } catch (err) {
        if (err.name !== 'AbortError') onError?.(new HermesError(`stream broke: ${err.message}`));
      }
    })();

    return { done, cancel: () => ctrl.abort() };
  }

  /* ── switchboard ────────────────────────────────────────────── */

  health()        { return this.request('/health'); }
  healthDetailed(){ return this.request('/health/detailed'); }
  capabilities()  { return this.request('/v1/capabilities'); }
  models()        { return this.request('/v1/models'); }
  toolsets()      { return this.request('/v1/toolsets'); }
  skills()        { return this.request('/v1/skills'); }

  /* ── runs: the work orders ──────────────────────────────────── */

  /**
   * @param {object} order
   * @param {string} order.prompt
   * @param {string} [order.model]
   * @param {string} [order.provider]
   * @param {object} [order.context]
   * @param {boolean}[order.approval_required]
   * @param {string} [order.session_id]
   */
  createRun(order) {
    return this.request('/v1/runs', { method: 'POST', body: order });
  }

  getRun(runId)  { return this.request(`/v1/runs/${encodeURIComponent(runId)}`); }
  stopRun(runId) { return this.request(`/v1/runs/${encodeURIComponent(runId)}/stop`, { method: 'POST', body: {} }); }

  steerRun(runId, guidance) {
    return this.request(`/v1/runs/${encodeURIComponent(runId)}/steer`, {
      method: 'POST', body: { guidance, message: guidance },
    });
  }

  approveRun(runId, approved, note = '') {
    return this.request(`/v1/runs/${encodeURIComponent(runId)}/approval`, {
      method: 'POST', body: { approved, decision: approved ? 'approve' : 'deny', note },
    });
  }

  runEvents(runId, handlers) {
    return this.stream(`/v1/runs/${encodeURIComponent(runId)}/events`, handlers);
  }

  /* ── sessions: the filing room ──────────────────────────────── */

  listSessions({ limit = 50, offset = 0 } = {}) {
    return this.request(`/api/sessions?limit=${limit}&offset=${offset}`);
  }

  createSession(body = {}) { return this.request('/api/sessions', { method: 'POST', body }); }
  getSession(id)      { return this.request(`/api/sessions/${encodeURIComponent(id)}`); }
  sessionMessages(id) { return this.request(`/api/sessions/${encodeURIComponent(id)}/messages`); }
  forkSession(id)     { return this.request(`/api/sessions/${encodeURIComponent(id)}/fork`, { method: 'POST', body: {} }); }
  deleteSession(id)   { return this.request(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }); }

  chat(sessionId, message, { model, provider } = {}) {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/chat`, {
      method: 'POST', body: { message, model, provider },
    });
  }

  chatStream(sessionId, message, handlers, { model, provider } = {}) {
    return this.stream(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`, {
      method: 'POST',
      body: { message, model, provider, stream: true },
      ...handlers,
    });
  }

  /* ── jobs: the night shift ──────────────────────────────────── */

  listJobs()            { return this.request('/api/jobs'); }
  createJob(job)        { return this.request('/api/jobs', { method: 'POST', body: job }); }
  patchJob(id, patch)   { return this.request(`/api/jobs/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch }); }
  deleteJob(id)         { return this.request(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
  pauseJob(id)          { return this.request(`/api/jobs/${encodeURIComponent(id)}/pause`, { method: 'POST', body: {} }); }
  resumeJob(id)         { return this.request(`/api/jobs/${encodeURIComponent(id)}/resume`, { method: 'POST', body: {} }); }
  runJob(id)            { return this.request(`/api/jobs/${encodeURIComponent(id)}/run`, { method: 'POST', body: {} }); }
}

/**
 * Turn one raw SSE frame into {event, data}. `data:` may span lines and
 * is JSON in every Hermes stream except the terminal `[DONE]` sentinel.
 */
export function parseSSEFrame(frame) {
  let event = 'message';
  const dataLines = [];

  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }

  if (!dataLines.length) return null;
  const raw = dataLines.join('\n');
  if (raw === '[DONE]') return { event: 'done', data: null, raw };

  try { return { event, data: JSON.parse(raw), raw }; }
  catch { return { event, data: null, raw }; }
}
