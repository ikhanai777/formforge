# Bullion — architecture

Confidence-scored, explainable buy/sell signals for gold (XAU/USD) and silver (XAG/USD).

This document is the map. It covers what the system does, where every decision lives, and —
most usefully for anyone picking it up — where it deliberately departs from the spec and why.

---

## 0. The framing constraint

The spec opens by refusing the brief as written, and that refusal is the product:

> "Highly accurate buy/sell signals" is the ask, but no system — human or algorithmic — can
> promise accuracy on XAU/XAG with any honesty.

So the build is arranged so that honesty is *structural* rather than a matter of discipline:

| The claim | Where the code enforces it |
|---|---|
| Confidence is published, not accuracy | `fusion.Confidence`, built from five inputs of which only one is the score |
| The system shows its reasoning | `scoring.LayerScore.reasoning` is part of the return type; a layer that cannot explain cannot vote |
| Every signal is probabilistic | `SignalState.NO_SIGNAL` exists and `Thresholds.confidence_floor` outranks the score |
| The track record is published | `scorecard.build` with Wilson intervals, sample sizes, and synthetic rows excluded |
| Not financial advice | `disclosure.annotate` on every response; `FORBIDDEN_PHRASINGS` is enforced by a test |

The last one is worth noticing: `tests/test_bullion_api.py` parses every string literal in the
package and fails the build if the product describes itself as "highly accurate", "guaranteed"
or "risk-free". Copy drifts; tests don't.

---

## 1. Layout

```
bullion/
  config.py        weights, thresholds, metal profiles, tiers — every tunable number
  series.py        OHLCV containers; closed-bar and ordering guarantees
  indicators.py    EMA/MACD/ADX/RSI/StochRSI/ATR/Bollinger, stdlib only
  structure.py     swings, support/resistance clustering, Fibonacci zones
  providers/
    base.py        the five normalised shapes every feed becomes
    http.py        retrying stdlib client + the free-tier MonthlyBudget
    prices.py      Twelve Data primary, GoldAPI/Metals-API cross-check, agreement maths
    macro.py       FRED (keyed JSON or keyless CSV) + price-feed DXY
    positioning.py CFTC COT parser, tolerant of column renames
    news.py        Finnhub/Marketaux, keyword-filtered
    synthetic.py   a deterministic labelled market for tests, CI and offline demos
  scoring/
    technical.py   seven sub-scores behind one ADX gate
    macro.py       real yields, dollar, CPI, curve — all as z-scored changes
    positioning.py COT momentum damped by a crowding percentile
    sentiment.py   recency- and relevance-weighted, shrunk by effective sample size
    llm_news.py    Claude Haiku re-scoring for metal direction; lexicon fallback
  regime.py        TREND / RANGE / EVENT detection and the weight tilt
  fusion.py        composite score, confidence, state, structure-aware levels
  backtest.py      walk-forward, frame caching, weight fitting, R-multiple grading
  scorecard.py     live grading, published buckets, drift z-test
  pipeline.py      one recompute cycle, fault-isolated per asset and per layer
  alerts.py        hysteresis, cooldown, invalidation
  store.py         sqlite implementing the TimescaleDB shape
  api/             FastAPI endpoints + tier gating
  webapp/          the single-file dashboard
  cli.py           signal / ingest / grade / backtest / scorecard / serve / demo
```

---

## 2. Data flow

```
                    ┌──────────────── providers ────────────────┐
  Twelve Data ─┐    │                                            │
  GoldAPI ─────┼──► prices.CrossValidatedPrices ──► Series       │
  Metals-API ──┘    │         (agreement + staleness)            │
  FRED ────────────► macro.FredMacro ─────────────► MacroSeries  │
  CFTC ────────────► positioning.Cftc ────────────► CotHistory   │
  Finnhub/Marketaux ► news.ChainedNews ───────────► Headline[]   │
                    └──────────────────┬─────────────────────────┘
                                       ▼
                            pipeline.collect → LayerData
                                       ▼
               indicators.compute + structure.levels + regime.detect
                                       ▼
      scoring.technical / macro / sentiment / positioning → 4 × LayerScore
                                       ▼
                    fusion.fuse(weights from regime tilt)
                                       ▼
                 Signal: state + confidence + reasoning + levels
                            ▼                        ▼
                    store (signals table)      alerts.evaluate
                            ▼                        ▼
              scorecard.grade_pending          alerts table (queue)
                            ▼
                   API → dashboard / track record
```

---

## 3. Decisions that differ from the spec, and why

### 3.1 TimescaleDB is the target; sqlite is what runs

`docs/bullion-schema.sql` is the Timescale schema, hypertables and compression policy
included. `bullion/store.py` implements the same tables and the same column names on stdlib
sqlite3, and `tests/test_bullion_store.py` asserts they agree column for column.

This is formforge's own pattern and the reason applies twice over here: the `signals` table is
the product's credibility, it cannot be backfilled, and a persistence layer that needs a
database server running is one that gets switched off in development. Collection has to be the
default.

### 3.2 Indicators are stdlib, not `pandas-ta`

The spec picks `pandas-ta` to avoid TA-Lib's C build step. Going one step further and writing
the maths in plain Python costs nothing at this scale (a few thousand bars, O(n)) and buys
three things: the file is readable top to bottom by anyone auditing a signal, the deploy has no
numeric wheels to pin, and there is no chance of two provider libraries disagreeing about what
RSI means after a version bump.

### 3.3 FRED needs a key for JSON — but not for CSV

The spec says FRED is "free, no key required for basic series". The JSON API does require a
free key. The `fredgraph.csv` endpoint that backs the public charts does not, so `FredMacro`
uses the CSV path when no key is configured and the JSON API when one is. Same data either way.

### 3.4 The cross-check tolerance is volatility-scaled

The obvious implementation of "never trust a single price source" compares a live spot quote
from feed B against the last closed bar from feed A. On a daily interval those are not supposed
to match — one is now, the other is yesterday's close — and a fixed 25bp tolerance produces a
permanent false alarm. `CrossValidatedPrices._allowance_bp` adds a typical bar's range, scaled
by how far into the current bar we are. Inside that envelope the feeds agree.

### 3.5 Not every recompute writes a signal row

The spec wants every signal tracked and graded. A five-minute loop writing a row per cycle
would make the published win rate an average over *recomputes* rather than over signals. So
`pipeline.should_persist` writes on a material change (state, or ≥10 points of confidence) or
on a four-hour heartbeat, and one row then means one signal.

### 3.6 Synthetic data is a first-class, heavily labelled mode

No price key means the engine runs on `providers.synthetic.SyntheticWorld`: a deterministic
regime-switching random walk with correlated gold and silver, matching macro series, COT
history and headlines. Labelled at every layer — provider name, `LayerData.synthetic`, a note
on the signal, `data_mode` on every API response, a banner on the dashboard, exclusion from the
default scorecard, and a refusal to print fitted weights as adoptable. `BULLION_ALLOW_SYNTHETIC=0`
refuses to construct it at all, which is the production posture.

---

## 4. The confidence model

Direction and confidence come from different inputs, on purpose (spec section 4).

```
composite = Σ wᵢ · scoreᵢ        over live layers, weights renormalised

confidence = (0.34·conviction + 0.28·agreement + 0.18·trend + 0.20·coverage [+0.06 unanimous])
             × (0.35 + 0.65·freshness)
             × (0.75 + 0.25·price_agreement)
```

| Term | Meaning |
|---|---|
| `conviction` | `\|composite\| / 0.7`, capped — how far from neutral |
| `agreement` | weighted mean deviation from the composite, **divided by the mean layer magnitude** |
| `trend` | ADX mapped 12→35 onto 0→1, saturating (ADX 55 is not a stronger mandate than 35) |
| `coverage` | fraction of total weight backed by live data |
| `freshness` | weight-weighted layer freshness; a multiplier, never a term |
| `price_agreement` | how well the two price feeds matched |

The relative-dispersion detail in `agreement` is load-bearing. Measured absolutely, one layer
at +1.0 against three at 0.0 scores 0.63 agreement — because three silent layers sit only 0.25
away from the resulting composite — and the engine publishes a single-layer call at 61%
confidence. Dividing by the mean layer magnitude asks the right question (is the spread small
*relative to the signal*?) and returns 0.

Both multipliers can only reduce. Good data cannot manufacture conviction; bad data removes it.

---

## 5. Backtesting

Walk-forward, never a single in-sample fit. Three properties make the number publishable:

1. **Frames are cached.** Layer scores do not depend on weights, so each bar is scored once and
   every candidate weight vector is a cheap re-fusion. That is what makes 81-candidate fitting
   affordable in pure Python — and it guarantees the fitted weights were evaluated against
   exactly the layer scores the live engine would have produced.
2. **History is sliced by timestamp.** `History.as_of(when)` is the only way the replay sees the
   past: series, macro points, COT reports and headlines are all filtered by date. Every
   lookahead bug is an off-by-one in a slice, so there is one slice and it takes a timestamp.
3. **Ties go to the stop.** A bar touching both levels grades as a loss. Without tick data the
   order is unknowable and resolving ties favourably is the most common way a backtest flatters
   itself.

Results are reported in **R** (multiples of the risk to the suggested stop) because it is the
only unit in which a gold trade and a silver trade are comparable. Each fold keeps both its
in-sample and out-of-sample numbers; `WalkForward.overfit_gap` is the difference, and it is the
most informative output the module produces.

A sanity check worth keeping: on the synthetic random walk the engine reports an out-of-sample
expectancy near zero and a win rate around 1/(1+R). That is the correct answer. A system showing
a large positive expectancy on noise has a lookahead bug, not an edge —
`test_no_edge_is_found_in_a_random_walk` asserts it.

---

## 6. The track record

`scorecard.build` reads graded rows and reports win rate, Wilson 95% interval, expectancy in R
and average 7-day return, bucketed by asset, strength, regime and confidence band. Deliberately
unflattering defaults:

- Buckets under 20 graded signals are marked unreliable and the dashboard renders them greyed.
- Open signals are `pending`, never counted either way.
- Synthetic rows are excluded and the count of exclusions is published.
- A win is defined by the *outcome*, not by the sign of the 7-day return: a trade stopped out on
  day two that recovered by day seven is a loss, because the user was out of it.

`scorecard.drift` runs a two-proportion z-test of live win rate against the last walk-forward's
out-of-sample rate, refuses a verdict below 20 resolved signals, and reports *outperformance* as
drift too — the backtest has stopped describing the live system either way.

---

## 7. Operating it

```bash
# Works with no credentials: synthetic world, labelled as such.
bullion signal

# Populate a store with synthetic history, signals and grades, then show the scorecard.
bullion demo

# Live data.
export TWELVEDATA_API_KEY=...      # primary: metals + DXY + yield proxies in one key
export GOLDAPI_KEY=...             # cross-check only, 500 req/month, budgeted on disk
export FRED_API_KEY=...            # optional; the keyless CSV path works without it
export FINNHUB_KEY=...             # or MARKETAUX_KEY; without one, sentiment is dark
export ANTHROPIC_API_KEY=...       # Haiku news re-scoring; without it, the lexicon
export BULLION_ALLOW_SYNTHETIC=0   # production: a missing price key should page someone

bullion ingest --bars 1200         # build history cheaply, no scoring
bullion signal --persist           # one cycle, stored and alerted
bullion grade                      # resolve matured signals (schedule this)
bullion backtest --persist         # walk-forward; prints an adoptable BULLION_WEIGHTS
bullion scorecard                  # the published record
bullion serve                      # API + dashboard on :8000
```

A reasonable schedule: `signal --persist` hourly, `grade` daily, `backtest --persist` weekly
(the spec's drift cadence).

### Tiers

Gating is server-side (`api/tiers.py`). The free tier's delay serves a *real older signal*
rather than the current one with a faked timestamp — that distinction is the difference between
a product and a lie. Withheld fields are replaced with a `locked` marker naming what the paid
tier adds, because a paywall the user cannot see reads as a bug.

---

## 8. What is not built

Stated plainly rather than implied:

- **Push transport.** Alerts are persisted to the `alerts` table as a queue with a `delivered`
  flag. No APNs/FCM client: credentials are a deployment concern and a stub that pretends to
  send is worse than an honest queue.
- **Authentication.** `X-Bullion-Tier` is a development affordance, not entitlement. A real
  deployment resolves the tier from an authenticated user.
- **Playwright scrapers** for FOMC statement text and World Gold Council flows (spec 3.3). The
  FOMC *calendar* is hardcoded from the published schedule — it changes once a year, and
  scraping it weekly is the wrong trade — but statement-tone scoring and WGC flow data are not
  ingested. The macro layer runs on four FRED series plus COT.
- **Intraday timeframes.** `series.resample` and the `interval` parameter support them and the
  store is keyed on interval, but the weights and the 7-bar horizon are tuned for daily swing
  signals. The Pro tier's multi-timeframe promise needs its own fit.
- **A live news archive for backtesting.** Free news tiers serve recent headlines only, so a
  multi-year backtest has no sentiment layer for most of its span. It scores unavailable for
  those bars and the weight redistributes — visibly, in the output, rather than silently.

---

## 9. Compliance

Spec section 11, implemented as code rather than intention: `disclosure.DISCLOSURE` is served at
`/v1/disclosure` and rendered persistently in the dashboard footer; `SHORT_DISCLOSURE` rides on
every API response and every alert; `FORBIDDEN_PHRASINGS` is enforced by a test; signal
headlines are descriptive, never imperative.

None of this is legal advice either. Have a lawyer read it before a public launch.
