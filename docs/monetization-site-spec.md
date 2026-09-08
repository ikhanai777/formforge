# FormForge web app — monetization spec

Specification only. Nothing in this document is built yet.

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

- B2B API/licensing surface for print farms (mentioned in §0's rejected
  table as a plausible phase-2, not designed here).
- Template marketplace / revenue share for third-party generator authors.
- Mobile native app — the mobile-responsive web work already done on
  `studio.html` this session covers the mobile web case; a native app is a
  separate, much larger bet with no signal yet that it's needed.
- Any change to which generators exist or what they cost computationally —
  pricing in §2 is a placeholder pending real measurement, not a geometry
  ask.

## 10. Open questions for the person who wanted this monetized

These are the calls only the user can make; everything above assumes an
answer and says which one, so any of these can flip a section without
starting over:

1. Does §0's framing (hosted paid generator, CLI stays free) match the
   intent, or was "monetizing FormForge" actually about the marketplace or
   B2B-licensing rows in that rejected-options table?
2. Is metered credits (§2) the right shape, or is a flat subscription with
   soft fair-use limits preferred (simpler to explain, harder to keep from
   losing money on heavy users)?
3. Free tier at 3 builds/month — too generous, too stingy, or fine as a
   starting guess pending §8's conversion data?
4. Any existing infra preference (Stripe is assumed for billing; AWS/GCP/
   Cloudflare for storage and compute) or is this greenfield?
