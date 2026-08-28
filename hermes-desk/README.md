# HERMES &amp; CO. — an agency floor for Hermes Agent

A retro dashboard for [Hermes Agent](https://hermes-agent.nousresearch.com/), the
open-source self-improving agent from Nous Research. It presents a running agent
as a **1980s clerical office**: work orders come in at the front desk, the floor
supervisor puts them on a worker, delegated subagents get their own desks
reporting into the lead, and everything is filed at the end of the day.

It is one static page and a small proxy. No build step, no npm, no framework.

```
python3 hermes-desk/server.py     # then open http://127.0.0.1:8777
```

It boots into **simulation** if no gateway answers, so you can look at the whole
interface before you wire anything up.

---

## The metaphor is the API

Nothing here is decorative-only. Every department is a real Hermes endpoint.

| Department | What you see | Hermes API |
|---|---|---|
| **The Floor** (F1) | A desk per live run, animated by its real status; subagents indented under the run that spawned them; each desk's little CRT shows the tool currently executing | `GET /v1/runs/{id}/events` (SSE), `GET /v1/runs/{id}` |
| **Work Orders** (F2) | The order pad and the ledger of everything dispatched | `POST /v1/runs`, `POST /v1/runs/{id}/stop` |
| **Dispatch** (F3) | A streaming conversation, with tool progress inline | `POST /api/sessions/{id}/chat/stream`, `POST /api/sessions` |
| **Personnel** (F4) | Models as staff you can assign, toolsets as departments and posts, skills as the training record | `GET /v1/models`, `GET /v1/toolsets`, `GET /v1/skills` |
| **Archives** (F5) | The filing room: every session, its transcript, and branching | `GET /api/sessions`, `.../messages`, `.../fork` |
| **Night Shift** (F6) | Unattended work on a cron rota | `GET/POST/PATCH/DELETE /api/jobs`, `.../pause|resume|run` |
| **Switchboard** (F7) | Wiring, line status, and what the gateway advertises | `GET /health/detailed`, `GET /v1/capabilities` |

Desk-level controls map onto the run control endpoints: **Steer** is
`POST /v1/runs/{id}/steer`, **Stop** is `.../stop`, and a desk that turns amber
with a **Sign** button is a run parked at `waiting_for_approval`, resolved through
`.../approval`.

Subagent desks appear because `delegate_task` returns a handle carrying
`subagent_ids`; the dashboard watches the parent's event stream for that handle
and opens a desk for each child, then attaches to the child's stream too. With
Hermes' default `delegation.max_concurrent_children: 3` you get a lead desk and
up to three subagent desks per team.

## Connecting it to a real Hermes

**1 — turn on the API server.** In `~/.hermes/.env`:

```bash
API_SERVER_ENABLED=true
API_SERVER_KEY=change-me-local-dev
```

**2 — start the gateway.** It listens on `127.0.0.1:8642` by default:

```bash
hermes gateway
```

**3 — serve the dashboard**, with the key held server-side:

```bash
HERMES_API_KEY=change-me-local-dev python3 hermes-desk/server.py
```

Open <http://127.0.0.1:8777>, go to **Switchboard (F7)**, and press *Test the
line*. The stamp in the letterhead flips from `SIMULATION` to `LIVE WIRE`.

### The three transports

Chosen in Switchboard, remembered in `localStorage`.

- **Proxy** (default, recommended) — the browser calls `/hermes/*` on
  `server.py`, which forwards to the gateway and adds
  `Authorization: Bearer $HERMES_API_KEY`. **The key never reaches the page.**
- **Direct** — the browser calls the gateway itself. Requires the key in
  `localStorage` and the gateway's CORS allowlist to name this origin:
  `API_SERVER_CORS_ORIGINS=http://127.0.0.1:8777`. Convenient; strictly worse
  for secrets. Use it only on a machine you own.
- **Simulation** — no network at all. `mock.js` fabricates runs, delegations,
  sessions and jobs *in the exact payload shapes the gateway emits*, so the
  adapters are exercised by realistic frames.

### Server environment

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_URL` | `http://127.0.0.1:8642` | Upstream gateway |
| `HERMES_API_KEY` | *(empty)* | Your `API_SERVER_KEY` |
| `HOST` / `PORT` | `127.0.0.1` / `8777` | Where the dashboard binds |

A multiplexed gateway (`gateway.multiplex_profiles`) routes everything under
`/p/<profile>/`; put the profile name in the Switchboard field and every call is
prefixed for you.

## Layout

```
hermes-desk/
  server.py              static files + /hermes/* → gateway, SSE streamed through
  index.html
  assets/css/theme.css   phosphor palettes, CRT overlay, retro chrome primitives
  assets/css/app.css     layout and per-department styling
  assets/js/
    api.js               HermesClient — every network call in the app
    adapters.js          Hermes payloads → the agency metaphor
    mock.js              the simulation transport
    store.js             state, the run ledger, event-stream tracking
    sprites.js           pixel workers, as inline SVG
    ui.js                DOM helpers and shared widgets
    views/*.js           one file per department
```

**If the Hermes API changes, two files move:** `api.js` (paths) and
`adapters.js` (payload readers). Nothing else knows a URL or a field name.

`adapters.js` is deliberately permissive about the loosely-specified parts. Run
status values and event `type` names are documented and read exactly; the `data`
blob inside a `run.progress` frame carries whatever the executing tool reported,
so those readers probe a list of plausible keys and fall back to something
displayable rather than throwing. Each reader names the keys it probes.

## Notes on the interface

- **F1–F7** switch departments, as the terminal it is imitating would.
- **AMBER / GREEN / IBM** in the status bar cycle the phosphor. Sprites are
  drawn from CSS custom properties, so they recolour with the theme.
- **♪** turns on short square-wave console tones for completions and failures.
  Off by default.
- The run ledger persists in `localStorage`, so a reload re-attaches to every
  still-running event stream instead of losing the floor.
- `prefers-reduced-motion` disables the CRT flicker and the desk animations.

## Limits worth knowing

- Hermes hands a `run_id` to whoever created it and has no "list all runs"
  endpoint, so **the floor shows runs dispatched from this browser**, plus any
  subagents they spawn. Runs started from the terminal UI or from Telegram will
  not appear.
- The subagent desks depend on `delegate_task`'s handle surfacing on the parent's
  event stream. If a gateway build does not emit it, the lead desk still works
  and simply shows no crew.
- `server.py` is a development server: single-purpose, bound to localhost, and
  not hardened for exposure to a network.
