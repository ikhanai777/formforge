# FormForge

Turns a sentence into a print-ready 3D model.

```
$ formforge generate "a wall planter 140mm wide and 100mm tall with drainage"

  [ok] intent    category=planter dimensions=height_mm=100, width_mm=140
  [ok] route     template via planter_halfmoon_wall (score 0.75)
  [ok] codegen   parameters filled
  [ok] execute   solid built: 140.0 x 70.0 x 128.0 mm, 1112 triangles
  [ok] validate  36 checks passed
  [ok] render    4 views rendered
  [ok] critique  the renders match the request

Built a wall planter -- 140 x 70 x 128 mm, 1112 triangles, 1 iteration, 8.2s.
bundle: out/9f30159.../bundle
```

## The one decision everything else follows from

**The model does not generate geometry. It writes parametric CAD code, and a
real kernel generates the geometry.**

```
prompt → Claude writes build123d → OCCT B-rep kernel → exact solid → STL/3MF/STEP
```

Image-to-3D mesh generators produce organic blobs with non-manifold edges, no
dimensional accuracy, and walls no nozzle can print. For keychains, organizers,
planters and wall decor — functional, dimensioned, hard-surface objects — that
is the wrong tool. Driving a CAD kernel instead gives you, for free:

| | Parametric | Mesh generation |
|---|---|---|
| Watertight | Guaranteed by the kernel | Sometimes |
| Exact dimensions | To the micron | "Roughly 60-ish" |
| Editable afterwards | Change a number, re-run | No |
| STEP export | Yes | No |
| Fillets, chamfers, threads | Native operations | Impossible |
| Organic sculptural detail | Weak | Strong |

That last row is the honest weakness, and the mitigation is the hybrid path in
`docs/architecture.md`: generated detail may only be booleaned onto a parametric
base that owns all the functional geometry.

## What you get

Every successful generation produces a **bundle**, not a file:

```
model.3mf       the primary download -- declares its units, carries print settings
model.stl       compatibility, with a README saying the units are mm
model.step      the exact CAD solid, openable in Fusion or FreeCAD
source.py       the script that built it, with every dimension a named constant
params.json     the values, plus the valid range for each
report.json     the full manufacturability report
previews/       six orthographic views, an isometric, and a section cut
```

`source.py` is the point. It runs standalone:

```bash
$ pip install build123d && python source.py     # rebuilds the identical solid
```

Change `BODY_L_MM = 70` to `90`, re-run, and you have a new model. That is what
a mesh generator structurally cannot ship, and it is why the STEP file and the
script are worth more than the STL.

## Install

```bash
pip install -e ".[all]"      # everything
pip install -e .             # geometry only, no model client or web server
formforge doctor             # what is installed, configured and safe
```

`doctor` is worth reading before anything else. It reports whether the geometry
sandbox isolates the host kernel, which matters more than any other line.

## Use

### Command line

```bash
formforge templates                              # what is available
formforge templates planter_halfmoon_wall        # parameters, ranges, print test
formforge generate "a hex planter for a 4in pot"
formforge build keychain_text_tag --set text=RIVER --set body_l_mm=70
formforge check model.stl --profile bambu_p1s_0.4 --category planter
formforge render model.stl --out previews/
formforge rules --profile prusa_mk4_0.4          # the DFM rules being applied
formforge stats                                  # what the recorded runs say
formforge feedback <model-id> --failed --issue warping
```

Every `generate` is recorded to a local SQLite database (`$FORMFORGE_DB`,
default `~/.formforge/formforge.db`) — the generation, its per-step log, and
any refusal. `formforge stats` reads it back: which templates are quietly
failing, which errors actually dominate, and whether anything printed. None of
those three can be answered retroactively, which is why collection is on by
default rather than behind a flag (`--no-store` opts out).

`formforge feedback` is the one that matters most and the one with no
substitute. Every DFM constant in this system is a conventional maker value;
a print outcome recorded against a model is the only thing that can make one
of them a measurement, and it lands next to what the validator measured at the
time.

### From Claude, over MCP

```bash
python -m formforge.mcp        # stdio server
```

Then ask Claude for "a hex wall planter for a 4-inch pot". The tool results
carry the preview images inline, so Claude can see what it made and correct
itself in the same turn.

`report_print_result` is the tool worth knowing about: when the user comes back
and says how a print came out, that sentence is the only empirical evidence this
system will ever have about its own thresholds, and it lands against the model
it describes.

### As a service

```bash
FORMFORGE_SANDBOX_RUNTIME=gvisor uvicorn formforge.api.app:app
```

`POST /v1/generate` returns immediately with a `model_id`; the WebSocket at
`/v1/models/{id}/stream` replays every step of the loop as it happens. The loop
is worth showing rather than hiding — watching it find a 1.1 mm wall and
regenerate is the clearest possible argument for the whole approach.

`GET /v1/models/{id}/events` replays it again afterwards, from the database.
`GET /v1/stats` reports template health and the dominant failure classes;
`POST /v1/feedback` takes a print outcome and `GET /v1/stats/prints` reads it
back beside what the validator measured at the time.

## How it works

```
1. INTENT      the request becomes a structured object; clarify only if a
               *functional* dimension is missing, never about style
2. ROUTE       vector search over the template registry
                 strong match  → fill a verified template  (fast, cheap)
                 near match    → freeform, seeded with that template
                 no match      → freeform from scratch  (5-10x the cost)
3. GENERATE    schema fill, or build123d written against the DFM rules
4. EXECUTE     sandboxed: no network, rlimits, ephemeral, kernel-isolated
5. VALIDATE    three tiers -- topology, printability, category invariants
6. CRITIQUE    render it and show the model its own output
7. REVISE      up to four attempts, one escalation, then a partial result
               with an explanation
```

Steps 5 and 6 do different jobs. Validation proves the mesh is *valid*; nothing
in it proves the mesh is *the thing that was asked for*. A perfectly manifold,
DFM-clean solid that looks nothing like a cat passes every numeric check and is
still a failure. Rendering the result and re-showing it catches mirrored text,
features buried inside the solid, and proportions that are individually correct
and jointly absurd.

## The validation engine

Three tiers, every check carrying its measured value and the threshold it was
compared against — because "wall too thin" is not actionable and "min wall
1.08 mm at (12.4, -3.1, 6.0), needs 1.2 mm" is.

- **Tier 1, topology.** Watertight, consistent winding, outward normals,
  self-intersection, degenerate faces, stray shards, genus sanity. Hard
  failures: a mesh that fails these is not a model with a problem, it is not a
  model.
- **Tier 2, printability.** Wall thickness by inward ray casting, feature size,
  hole diameters read exactly from the B-rep, build volume, overhang area,
  bridge spans, first-layer contact, tipping stability, trapped volume, text
  legibility. Thresholds resolve from the printer and material, so the same
  geometry passes on a 0.4 mm nozzle and warns on a 0.6 mm one.
- **Tier 3, category invariants.** A planter that is watertight, printable and
  has no drainage hole passes every generic check and is still a bad planter.

Templates declare their own **preconditions** (relationships between parameters,
checked before building) and **invariants** (properties of the measured
geometry, checked after). Keeping them apart matters: a JSON Schema can only
constrain one number at a time, so "the text has to fit on the tag" has nowhere
else to live, and checking it afterwards reports "the geometry is broken" when
the truth is "those two numbers cannot both be right".

## Security

The sandbox executes model-authored Python. That is the entire threat model.

**The container is the boundary.** Everything else raises the cost of the
obvious attacks without containing a determined one:

- No network. A prompt injection that succeeds in running arbitrary code still
  has nowhere to send anything.
- Read-only rootfs, a tmpfs at `/work`, empty environment, dropped capabilities,
  non-root, pid limit, CPU and memory rlimits, one job per container, destroyed
  after.
- gVisor or Firecracker in production. Plain Docker shares the host kernel.
- A static AST gate rejects disallowed imports, dynamic execution, dunder access
  and unbounded loops *before* a container is spawned — and a guarded
  `__import__` catches the dynamically-constructed names the static scan cannot.

The `subprocess` runtime used for local development has **no filesystem or
network isolation at all**. The gate blocks `open()`, but numpy and trimesh are
on the import allowlist and both write files, so a script can put bytes anywhere
the host user can. A test pins that fact in place so nobody mistakes the gate
for containment.

The AST gate is defence in depth, not the primary control. Assume it is
bypassable. **The API refuses to start when the runtime does not isolate the
host kernel**, because shipping the development path to production is the single
most likely way this system gets someone owned, and that belongs in code rather
than in a runbook.

## Testing

```bash
pytest                                             # the suite
python -m formforge.eval.check_templates           # every template builds
python -m formforge.eval.check_templates --extremes  # ...at every range extreme
python -m formforge.eval.benchmark                 # the metrics from the spec
python -m formforge.eval.benchmark --baseline docs/benchmark-baseline.json
```

`docs/benchmark-baseline.json` is the last committed run, and `--baseline`
fails when a metric drops against it. Almost every change here is to a prompt,
a DFM constant or a template — none of which have types, and all of which
regress silently — so the baseline is the type system they do not have. Update
it in the same commit as the change that moves it, and say which metric moved.

The template harness is not optional tooling. A schema that permits a 200 mm
planter is a promise that a 200 mm planter builds, and the sweep is what holds
the registry to it — it found the grazing-ray artifact, the annulus bridge false
positive and the coplanar-union bug that no unit test would have.

Everything runs with no API key. The template path is fully functional offline:
lexical matching picks a template, regexes pull dimensions and quoted text out
of the prompt, and schema defaults fill the rest. That is the route most traffic
should take anyway, so the system degrades to "templates only" rather than to
"broken".

## Layout

```
formforge/
  dfm.py          printer/material profiles, thresholds, the cached rules prompt
  security.py     the static AST gate
  binding.py      parameters bound by rewriting constants, comments intact
  hints.py        OCCT errors mapped to causes a model can act on
  policy.py       IP and safety screening, before any geometry
  registry.py     the template registry, matching and routing
  store.py        the tables that cannot be backfilled
  sandbox/        isolated execution and the in-sandbox runner
  validation/     the three tiers and the measurements behind them
  render/         numpy rasteriser, PNG encoder, section cuts
  orchestrator/   intent, codegen, critique, the loop
  mcp/            the MCP server
  api/            the HTTP gateway
  eval/           the template harness and the benchmark
  templates/      12 verified parametric definitions
```

`docs/architecture.md` covers the parts that need more than a paragraph:
tessellation, the measurement approximations and where they are wrong, the
repair ladder, and the deployment topology.

---

# Bullion — gold & silver signal engine

A second product in this repository, sharing nothing with FormForge but the build
conventions. Confidence-scored, explainable buy/sell signals for gold (XAU/USD) and
silver (XAG/USD).

Illustrative output — the shape of a call, not a real one:

```
$ bullion signal

XAU/USD  BUY
  confidence      68%  (composite +0.41)
  price           2,412.30
  invalidation    2,388.60   target 2,455.10   R:R 1.8
  level basis     ATR 16.20 x 1.5 stop, 1.8R target; stop placed just beyond
                  support 2,389.00 (4 touches)
  regime          trend — ADX 31 and rising, technical weighted up
  confidence from 74% layer agreement; trend strength 83%; 100% of weight backed
                  by live data; all layers agree on direction
  reasoning:
    technical    [w=43%] +0.61 — EMA20>EMA50>EMA200, RSI 58 (room to run), ADX 31
                                 (trending); MACD histogram positive and rising
    macro        [w=34%] +0.78 — 10Y TIPS real yield falling (-14bp/20d, -1.6σ);
                                 Broad dollar index falling (-0.9%/20d, -1.3σ)
    sentiment    [w=10%] +0.34 — 11 Claude-scored headlines in 72h, net bullish
    positioning  [w=13%] +0.46 — large specs adding net length (+22,400/4w),
                                 61st percentile of 3 years — not yet crowded

(Informational only, not financial advice. Signals are probabilistic and can be wrong.)
```

## The one decision everything else follows from

**Nothing in this system claims accuracy.** The brief asked for "highly accurate
signals"; no system can honestly promise that on metals driven by real yields, central
bank policy and geopolitical shocks. So the product is the honest version instead:
**confidence-scored, explainable, and self-graded in public.**

That is not a disclaimer bolted on at the end — it is the architecture:

| The claim | What enforces it |
|---|---|
| Confidence, not accuracy | Confidence is computed from five inputs, only one of which is the score. One screaming layer against three shrugs is a *low* confidence call. |
| It shows its reasoning | The reason string is part of each layer's return type. A layer that cannot explain does not vote. |
| It can say nothing | `NO_SIGNAL` is a state. Below the confidence floor, no directional call is published — whatever the score says. |
| The record is public | Win rate by strength, regime, asset and confidence band, each with a Wilson interval and a sample size, in the app's first screen. |
| Not financial advice | The disclosure rides on every response; a test fails the build if the product ever calls itself "highly accurate", "guaranteed" or "risk-free". |

## Four layers, one score

Each layer normalises to −1…+1 and explains itself:

- **Technical** — seven sub-scores (EMA stack, MACD, RSI, Stoch RSI, Bollinger, swing
  structure, ETF volume) behind an **ADX gate**. ADX gates rather than votes: in chop the
  trend opinion shrinks toward zero instead of generating the textbook whipsaw. The same
  reading flips how the band position reads — at the upper band with ADX 32 price is
  breaking out; with ADX 11 it is at the top of a range.
- **Macro** — real yields, the dollar, CPI and the curve, every one scored as a *z-scored
  change* rather than a level. A 1.9% real yield is neither bullish nor bearish; a 25bp
  fall when the typical monthly move is 8bp is strongly bullish.
- **Sentiment** — headlines keyword-filtered, then re-scored by Claude Haiku for *metal
  direction* rather than tone ("Fed holds, signals two cuts" names no metal and is very
  bullish gold). Recency-decayed, relevance-weighted, and shrunk toward zero by effective
  sample size so a quiet news day cannot produce a loud reading.
- **Positioning** — CFTC COT net speculative length, damped by a crowding percentile and
  turning *contrarian* at the extremes. The only non-monotonic layer, and the reason it
  earns its place.

Weights are not fixed: they are fitted by the walk-forward backtester and tilted by
detected regime (TREND / RANGE / EVENT — an FOMC decision in 18 hours outranks a clean
chart). Silver gets its own profile, not a copy of gold's.

## Backtesting is not an afterthought

Walk-forward only — train on one window, validate on the next, roll forward. Each fold
keeps *both* its in-sample and out-of-sample numbers, so `overfit_gap` is visible rather
than hidden. Results are in R (multiples of the risk to the suggested stop), the only unit
in which a gold trade and a silver trade compare.

Three properties keep the number publishable: layer scores are cached per bar so fitting
re-fuses exactly what the live engine would have produced; history is sliced only by
timestamp, so no bar can see its own future; and a bar touching both stop and target grades
as a **loss**, because without tick data the order is unknowable and resolving ties
favourably is how a backtest flatters itself.

A sanity check the suite asserts: on a random walk the engine finds **no edge** —
out-of-sample expectancy near zero, win rate near 1/(1+R). A system reporting a large
positive expectancy on noise has a lookahead bug, not an alpha.

## Use

```bash
# Works immediately, with no credentials: a deterministic synthetic market,
# labelled SYNTHETIC at every layer including the dashboard banner.
bullion signal
bullion demo                 # fill a store, grade it, print the scorecard
bullion serve                # API + dashboard on http://127.0.0.1:8000

# Live data. Only the first is required.
export TWELVEDATA_API_KEY=...   # primary: metals + DXY + yield proxies, one key
export GOLDAPI_KEY=...          # cross-check only; 500 req/month, budgeted on disk
export FRED_API_KEY=...         # optional — the keyless fredgraph CSV path works too
export FINNHUB_KEY=...          # or MARKETAUX_KEY; without one, sentiment goes dark
export ANTHROPIC_API_KEY=...    # Haiku news re-scoring; without it, a keyword lexicon
export BULLION_ALLOW_SYNTHETIC=0   # production: a missing price key should page someone

bullion ingest --bars 1200      # build history cheaply, no scoring
bullion signal --persist        # score, store, alert  (hourly)
bullion grade                   # resolve matured signals  (daily)
bullion backtest --persist      # walk-forward; prints an adoptable BULLION_WEIGHTS  (weekly)
bullion scorecard               # the published record, with drift verdict
```

A missing key degrades **one layer**, never the engine: the weight redistributes to the
other three and confidence drops to reflect the lost coverage. Only a missing price feed is
fatal — and with `BULLION_ALLOW_SYNTHETIC=0` it fails loudly rather than serving invented
bars.

## Dashboard

Dark, single file, no build step and no CDN — it renders on an offline demo and on a
locked-down network. The confidence gauge is the headline rather than the Buy/Sell word; the
four-layer bars make the *why* scannable; the "tide" strip aligns price, scored news
sentiment, issued signals and scheduled macro events on one axis; and the track record is on
the first screen, not in settings.

## Stack

Python 3.11, **standard library only** for the engine. No numpy, no pandas, no `pandas-ta`:
the maths is O(n) over a few thousand bars, so a numeric dependency buys nothing but a wheel
to pin and a build step on the deploy target — and the indicator code stays readable top to
bottom, which is the product's core claim about itself. FastAPI and `anthropic` are optional
extras; TimescaleDB is the target schema (`docs/bullion-schema.sql`) with a column-for-column
sqlite implementation that actually runs, so collection starts on day one instead of when
someone provisions a database.

## Layout

```
bullion/
  config.py       weights, thresholds, metal profiles, tiers — every tunable number
  series.py       OHLCV containers; ordering and closed-bar guarantees
  indicators.py   EMA/MACD/ADX/RSI/StochRSI/ATR/Bollinger, stdlib only
  structure.py    swings, support/resistance clustering, Fibonacci zones
  providers/      price (cross-validated), FRED macro, CFTC COT, news, synthetic world
  scoring/        the four layers + the Haiku news pass and its lexicon fallback
  regime.py       TREND / RANGE / EVENT detection and the weight tilt
  fusion.py       composite, confidence, state, structure-aware stop and target
  backtest.py     walk-forward, frame caching, weight fitting, R-multiple grading
  scorecard.py    live grading, published buckets, drift z-test
  pipeline.py     one recompute cycle, fault-isolated per asset and per layer
  alerts.py       hysteresis, cooldown, invalidation
  store.py        sqlite implementing the TimescaleDB shape
  api/            FastAPI endpoints + server-side tier gating
  webapp/         the single-file dashboard
```

`docs/bullion-architecture.md` covers the confidence model term by term, every place the
build departs from the spec and why, and — stated plainly rather than implied — what is not
built yet.

## Compliance

Informational and educational only; not investment advice, not a recommendation, not
personalised. The engine says "signal", "score" and "confidence", never "recommendation" or
"advice". Worth a lawyer's hour before a public launch — this README is not one.
