# FormForge web app — monetization spec and phased plan

Specification and implementation plan only. **Nothing is built yet — each
phase in §11 requires explicit approval before work on it starts.**

Decided (2026-09-08): hosted paid studio is the model, the free technical
path (CLI/MCP/offline templates) stays free, launch targets individual
makers on credits, and B2B licensing is deliberately deferred to a later
phase pending demand evidence rather than designed now. §10's open
questions 1 and 2 are resolved by this; 3 and 4 remain open and are called
out again at the point in §11 where they'd first block a phase.

"Monetizing FormForge" is not one product — it could mean a hosted generator
people pay to use, a marketplace for templates, or a licensing deal with
print farms. This spec picks the option that fits what already exists and
says so explicitly, so the assumption is easy to overturn rather than buried.

## 0. The assumption this spec is built on

**FormForge becomes a hosted app: people pay for generations and exports,
not for the software.** Concretely: `web/studio.html`'s six generators go
from "a file you open locally" to a real site with accounts, usage limits
tied to a plan, and payment. The CLI, MCP server and offline template path
stay free and open — this is about the thing a non-technical buyer would
actually pay for, which is a finished STL without installing Python.

Rejected alternatives, and why:

| Option | Why not the lead |
|---|---|
| Template marketplace (third parties sell templates) | No supply yet — there are 18 templates and one author. Revisit once outside contributors exist. |
| B2B API licensing to print farms / Etsy sellers | Real option, but it's a sales motion, not a spec for a website. Section 9 covers it as a second surface once the consumer site has traffic to prove demand. |
| One-time desktop app purchase | Undercuts the CLI, which is already free and open source; charging for the same binary is not a credible position. |
| Ads-supported free tool | Free-tier traffic on a CPU/memory-bound geometry sandbox is a cost center, not a monetizable audience, without a paid tier funding the compute. |

If the actual intent was one of the rejected rows, say so and this spec
gets rewritten around it — the architecture below (particularly the sandbox
and billing sections) changes accordingly.

## 1. Who pays, and for what

Two buyer profiles, both already implied by the generator set (vase, cup
holder, candle holder, watchtower, mushroom, sculpt vase):

- **Maker/hobbyist.** Owns a printer, wants a customized decorative object
  in five minutes without installing Python or learning build123d. Pays for
  convenience and the STL/3MF/STEP bundle. Price-sensitive, high volume.
- **Small seller.** Runs an Etsy/print-farm storefront, wants variety —
  a batch of 20 unique vases to list — without designing each by hand. Pays
  for volume and the reproducibility story (`source.py` + `params.json`
  means a listing can be regenerated identically if a customer wants the
  exact one pictured, or intentionally varied if they want "a vase like
  this one but taller"). Cares about bulk/batch export and commercial-use
  licensing terms.

Both profiles are served by the existing generator set; neither requires
new geometry work to launch.

## 2. Business model

**Metered credits, not seat-based subscription**, because the cost driver
is compute (sandboxed build123d executions bounded by CPU/wall/memory
limits — see `docs/architecture.md` §4), not per-user seats. A credit =
one successful, validated generation that produces a downloadable bundle;
failed/rejected builds (precondition violation, DFM failure) do not consume
a credit, matching the existing CLI/API behavior where a rejection is not
a billable event.

| Tier | Price (assumption, tune from real COGS) | Credits/mo | What's gated |
|---|---|---|---|
| Free | $0 | 3 | STL only, watermark-free — low enough to not be the product, high enough to prove the loop works before paying |
| Maker | $9/mo | 60 | STL + 3MF + STEP + source.py, all six generators |
| Studio | $29/mo | 300 | Everything in Maker + batch export (generate a population, download as one zip), priority queue |
| Pay-as-you-go | $0.30/credit | — | For anyone over their monthly allotment, no plan required |

Rollover: unused credits do not carry over (keeps the queue-capacity math
tractable — see §6). Overage on a paid plan falls through to
pay-as-you-go pricing rather than a hard stop, because a hard stop mid-batch
for a Studio customer is the worst possible moment to lose them.

**Explicitly not modeled at launch**: annual plans, team/org seats,
white-label embedding. All are reasonable phase-2 additions once the
credit-metering plumbing exists; adding them now multiplies the billing
surface before there's revenue data to justify it.

## 3. What's licensed to the customer

This needs a real answer before checkout exists, not after:

- The **generated model** (STL/3MF/STEP/source.py) is licensed to the payer
  for commercial use, unlimited seats, no attribution required — the
  print-farm buyer persona in §1 doesn't work otherwise.
- FormForge (the software, the templates, the generator definitions) is
  not sold or sublicensed — the customer owns their specific output, not
  the machine that made it.
- No claim of exclusivity: two customers can independently land on the same
  seed/parameter combination and get the same geometry. This should be
  stated plainly in the ToS rather than discovered by a customer comparing
  listings.

## 4. Site map

```
/                      landing: what it makes, live-ish gallery, pricing, CTA
/app                   the studio itself (see §5) — requires auth past the
                       free-tier credit ceiling, not before it
/pricing               tier comparison, FAQ (credits, overage, refunds)
/account
  /account/billing     plan, usage this period, payment method, invoices
  /account/history     past generations: thumbnail, params, re-download,
                       re-run-with-changes
/gallery               public feed of shared generations (opt-in per job) —
                       doubles as unpaid marketing surface
/docs                  same content as this repo's README/docs, web-rendered
/legal/terms
/legal/privacy
/legal/licensing       the §3 answer, in plain language
```

`/app` is a hosted, account-aware evolution of `web/studio.html` — same six
generator tabs, same live preview, same DFM readout — not a rewrite. The
existing client-side JS mesh builders (mirrored from the Python templates,
per the architecture summary above) stay as the *preview* path; export past
the free tier calls the real backend so the download is the actual
validated build123d/OCCT output, not the JS approximation. This distinction
must be visible to the user (a "preview" vs "final" label), because the two
are deliberately not pixel-identical — the JS mesh builders exist for zero-
latency slider feedback, not as a second source of truth.

## 5. Core flow

1. Land on `/app`, pick a generator tab (defaults to whatever `?g=` the
   landing page CTA pointed at).
2. Move sliders, get the existing live client-side preview — free, no
   account, no credit spent, exactly like today's `studio.html`.
3. Click "Build" → this is the paid action. Anonymous users get 3 free
   builds tracked by a signed cookie (not IP — the sandbox already has no
   idea what a "user" is, so this is new infrastructure regardless of
   mechanism); after that, prompted to sign up.
4. Build request goes to the real backend, through the existing
   orchestrator path (`formforge.api.app`) — sandboxed execution, three-tier
   validation, unchanged. Job id returned immediately, status streamed over
   the existing WebSocket (`/v1/models/{id}/stream`), same as the CLI/API
   today.
5. On completion: preview renders (the existing PNG previews, not the JS
   mesh), DFM report shown, download buttons per format, "generate another
   like this" (re-opens the same params in `/app`), "share to gallery"
   toggle.
6. Credit is deducted on successful validation, not on request — matching
   the "rejections aren't billable" rule in §2.

Batch (Studio tier): same flow, but the generator's population mode
(`--count N --seed S`) drives it — N specimens, one zip, N credits.

## 6. Backend / infra changes this requires

Everything in this section is new work on top of what exists; nothing here
proposes changing the generation pipeline itself.

- **Auth + billing**: not built today (`docs/architecture.md` §5 lists this
  explicitly as absent). Needs a `users` table — already anticipated in
  `docs/schema.sql` as a Postgres-only piece not yet implemented — plus a
  payments processor (Stripe: subscriptions for tiers, metered billing for
  overage) and a credits ledger (append-only, so a support dispute is
  answerable from history rather than a mutable balance).
  - **Reused as-is**: `generation_events`, `print_feedback` tables and the
    whole validation/report pipeline. Billing sits beside this data, not
    inside it — the geometry tier must still hold no credentials
    (`docs/architecture.md` §4), so the credit check and deduction happen
    in the orchestrator/gateway tier, never in the sandboxed worker.
- **Postgres migration**: SQLite was the right choice for a credential-free
  local tool; a multi-tenant paid product needs concurrent writes, the
  `users` table, and the embedding column already scoped in
  `docs/schema.sql`. This is the one piece of "what's not built" in the
  architecture doc that a monetized site forces, rather than merely
  motivates.
- **Queueing/autoscaling**: the geometry tier is already designed for this
  (gVisor, 2 vCPU/4 GB, one job at a time, autoscaled on queue depth) —
  the site adds a queue-depth-aware "N people ahead of you" status rather
  than new sandbox architecture. Paid-tier priority queue (§2, Studio) is
  a second queue with the same workers, not new infra.
- **Object storage**: bundles (STL/3MF/STEP/previews) need to live
  somewhere downloadable for the account history in §4, not just written to
  a local `out/` directory. S3-compatible storage with signed URLs, TTL
  on free-tier outputs (say 30 days) to bound storage cost, no TTL on paid
  history.
- **Rate limiting distinct from credits**: a credit limits spend; a rate
  limit protects the sandbox from someone scripting 300 rapid builds inside
  one credit-legal minute. Needed even on paid tiers.

## 7. What does NOT change

Worth stating so this doesn't get read as "rewrite FormForge":

- The generator/template registry, DFM validation, sandbox, and the CLI/MCP
  paths are untouched. The site is a new *front door and a bill*, not a new
  geometry engine.
- The free, offline, no-API-key template path stays free and unauthenticated
  forever — it's the reliability story and the thing that makes this
  auditable/open-source-credible, and undermining it to force people
  through checkout would contradict the project's own stated positioning
  ("degrades to templates-only rather than broken").
- `web/studio.html` (the local, no-server, no-account file) keeps existing
  and keeps working exactly as it does today. The hosted `/app` is an
  additional surface, not a replacement.

## 8. Metrics that decide if this worked

- Free→paid conversion rate (target range to be set after real free-tier
  usage data exists — no invented number belongs here yet).
  the compute cost per generation (from the sandbox's own CPU/wall
  measurements) must underprice the cheapest paid tier's per-credit cost
  with real margin, or the pricing table in §2 is wrong before launch.
- Credit utilization by tier (are Maker subscribers using ~60/mo, or
  massively over/under — either signals the tier boundary is wrong).
- Gallery share rate (proxy for whether the output is good enough that
  people want their name near it).

## 9. Explicitly out of scope for this spec

- B2B API/licensing surface for print farms — deliberately deferred past
  Phase 3 (§11), pending the demand signal Phase 3's metrics (§8) produce.
  Not designed here at all, and not one of the numbered phases below.
- Template marketplace / revenue share for third-party generator authors.
- Mobile native app — the mobile-responsive web work already done on
  `studio.html` this session covers the mobile web case; a native app is a
  separate, much larger bet with no signal yet that it's needed.
- Any change to which generators exist or what they cost computationally —
  pricing in §2 is a placeholder pending real measurement, not a geometry
  ask.

## 10. Open questions — status

1. ~~Hosted paid studio vs. marketplace vs. B2B~~ — **resolved**: hosted
   studio, per the 2026-09-08 decision at the top of this document.
2. ~~Metered credits vs. flat subscription~~ — **resolved**: metered
   credits, per the same decision.
3. Free tier at 3 builds/month — still open, no data to set it by yet.
   Phase 1 (§11) ships with this as a guess and §8's metrics are what
   should move it, not another guess.
4. Infra preference (Stripe assumed for billing; cloud provider assumed
   generic S3-compatible storage) — still open. This blocks Phase 0 concretely
   (§11) and should be answered before that phase is approved, since it
   changes which SDKs get pulled in.

## 11. Phased implementation plan

Each phase below ends with a **gate**: a concrete, demo-able state, and a
line that says what approval unlocks the next phase. No phase's work
starts before its gate is explicitly approved — including Phase 0. Phases
are sized to be independently shippable, not to be equal effort.

B2B licensing (the rejected-for-now row in §0) is not a phase here at all
— per this decision, it's a candidate to *design* only after Phase 3's
metrics (§8) show individual-maker demand, at which point it gets its own
spec, not a slot in this plan.

### Phase 0 — Foundations (no user-visible product yet)

The plumbing every later phase depends on, built once so it isn't
retrofitted under a paying customer.

- Postgres migration: bring up the schema in `docs/schema.sql` (users
  table added, `vector`/`citext` types), point `formforge.store` at it
  behind the same interface SQLite implements today. Existing SQLite
  deployments (CLI users) are unaffected — this is additive, not a cutover
  of the open-source path.
- Auth: email+password or OAuth (needs open question 4 answered — which
  provider) with sessions, no product surface wired to it yet beyond a
  bare login/signup page.
- Credits ledger: append-only table (`user_id`, `delta`, `reason`,
  `job_id?`, `created_at`); a `balance` view, not a stored counter, so a
  dispute is answerable from history per §6.
- Billing skeleton: Stripe customer + subscription objects wired to the
  three tiers in §2, webhook handling for renewal/cancellation/payment
  failure. No usage-based deduction logic yet — just "does this account
  have an active paid plan."
- **Gate**: a developer can sign up, subscribe to a fake $1 test plan via
  Stripe test mode, and see their credit balance in a database row. Nothing
  a real customer sees. Unlocks Phase 1 approval.

**Status: landed, with two deliberate departures.** `formforge/accounts/`
implements plans, identities, sessions, the append-only ledger and the
processor interface; `docs/schema.sql` carries the Postgres target for all
of it, held in step by a column-parity test per dialect. The gate is met in
substance — signup grants an opening balance, a subscription event grants a
month, spending debits it, and the balance is a sum over the ledger — with
these differences from the text above, both of which are about not shipping
unverified code:

- *No Postgres driver.* The schema half of the migration is done (tables,
  views, the `citext` case-folding reproduced in Python). The driver half is
  not, because there is no Postgres server in the development environment to
  verify one against, and a database backend that has never connected to a
  database is the kind of thing that looks finished and is not. Still a
  dialect change, and still the first thing Phase 1 needs.
- *No Stripe adapter, so the gate ran on the offline provider rather than
  Stripe test mode.* Open question 4 is still unanswered, and naming the
  processor in the code is how that decision gets made by accident. What
  exists instead is the interface plus a working offline implementation with
  real HMAC signature verification — so the renewal, cancellation,
  failed-payment and top-up handlers are tested, and answering question 4
  means writing one adapter against a handler that already works. Substituting
  the offline provider for Stripe test mode is a weaker gate than the text
  asked for, and it is named here rather than quietly counted as a pass.
- *No login or signup page, and nothing calls any of this.* The phase allowed
  "a bare login/signup page" as its ceiling and its gate required no user
  surface, so this is within scope rather than a shortfall — but it is worth
  being precise: `formforge/accounts/` is a library with tests, the HTTP
  gateway still has no authentication, and no request anywhere currently
  passes through a credit check. Wiring it up is Phase 1.

Two things worth flagging because they changed shape on contact:

- The `users` table in `docs/schema.sql` previously declared a mutable quota
  counter (`quota_used`, `quota_reset_at`). That is a different model from
  the credits this spec approved, and keeping both would have left two
  competing answers to "can this user build". The counter is gone; the
  ledger is authoritative.
- No-rollover (§2) is implemented as an `expiry` row for the remainder, not
  as a reset. A customer asking where their credits went gets an entry with
  a timestamp rather than a number that changed.

### Phase 1 — MVP hosted studio, one paid path

The smallest version of §4/§5 that actually charges someone money.

- `/app`: existing `studio.html` generator tabs adapted to call the real
  backend (`formforge.api.app`) for the "Build" action instead of only the
  JS preview mesh, per §4's preview-vs-final distinction.
- Anonymous free tier: 3 builds tracked by signed cookie (§5 step 3), then
  a signup wall.
- One paid tier live end to end: Maker only (Studio tier and batch export
  deferred to Phase 3) — subscribe, consume credits per successful build,
  see remaining balance, hit a paywall at zero with a clear "upgrade or
  buy credits" path (pay-as-you-go from §2 included here, since it's the
  overage backstop for the one tier that exists).
- Object storage for bundles (STL/3MF/STEP) with signed download URLs;
  30-day TTL on free-tier outputs.
- Minimal `/account/billing` (plan, balance, invoices) — no
  `/account/history` gallery/re-run features yet, those are Phase 3.
- Rate limiting on the build endpoint, independent of credit balance
  (§6's last bullet) — this is a security/cost floor, not a nice-to-have,
  and ships with the MVP rather than after an incident.
- **Gate**: a real external user can sign up, pay a real card via Stripe
  live mode, generate and download a real validated STL, and get correctly
  blocked at zero balance. This is the first phase a non-team member could
  actually use. Unlocks Phase 2 approval.

### Phase 2 — Launch readiness

Turns the working MVP into something safe to point traffic at.

- `/`, `/pricing`, `/legal/terms`, `/legal/privacy`, `/legal/licensing`
  (§3's answer written into real copy, reviewed — licensing terms are a
  legal document, not just a doc-comment, before real customers rely on
  them commercially).
- Queue-depth status ("N ahead of you") wired to the existing
  autoscaled-on-queue-depth geometry tier (§6) — no new sandbox
  architecture, just surfacing what it already tracks.
- Basic observability: the §8 metrics (free→paid conversion, credit
  utilization, per-generation compute cost vs. per-credit price) wired to
  dashboards, not just described. The pricing table in §2 is a guess until
  this exists.
- Load/cost validation: confirm the sandbox's measured CPU/wall time per
  generator (already produced by `check_templates.py` sweeps) times
  expected concurrent users doesn't outrun the geometry tier's autoscaling
  ceiling before real spend is at risk.
- **Gate**: the site is publicly linkable, legally reviewed, and its own
  dashboards show unit economics that make sense (cost per credit under
  price per credit, with real margin). Unlocks Phase 3 approval.

### Phase 3 — Full individual-maker feature set

Everything in §2/§4 that Phase 1 deferred, added once the MVP has proven
people will pay at all.

- Studio tier (§2): batch export via the generator's existing population
  mode (`--count N --seed S`), priority queue as a second queue on the
  same workers (§6).
- `/account/history`: past generations, thumbnails, re-download,
  "re-run with changes."
- `/gallery`: opt-in public feed, doubling as marketing surface (§8's
  share-rate metric starts meaning something here).
- Revisit the Phase 1 pricing guesses (free-tier build count, per-tier
  credit counts) against real Phase 2 dashboard data rather than the
  placeholders in §2's table.
- **Gate**: the product matches this spec's §2 pricing table and §4 site
  map in full for the individual-maker persona (§1). At this point §8's
  demand signal is real enough to decide whether a B2B spec is worth
  writing — that decision, and any B2B work, is explicitly not part of
  this plan.
