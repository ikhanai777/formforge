# FormForge Studio SaaS — specification (v1)

**Status: prototype live.** This describes a working product, not a proposal —
a hosted, account-gated front door onto FormForge's six parametric
generators, shipping today as a self-contained Claude Artifact.

## 1. Overview

Studio SaaS is the account layer around `web/studio.html`: sign up, land on
a dashboard, open the real Studio, design a model on real sliders with a
real live preview, and download a real STL — without installing anything or
standing up a server.

The distinguishing decision is *where it runs*. Rather than a conventional
client/server web app, v1 ships as a single Claude Artifact: one HTML
document holding the account shell, the unmodified Studio front end, and no
backend at all. Persistence and file delivery come from two Claude-hosted
capabilities — `db` for accounts, `downloads` for exports — instead of a
database server and an object store. That trade is the spine of this
document: it made a working, phone-reachable product possible in one
sitting, and §9 and §11 exist to grow out of it before real money or real
customer data touch it.

**Read this before the rest.** Everything under "shipped" in §3 is real and
clickable today. Everything under "planned" is scoped, not built. The two
are kept in one table on purpose — a spec that only describes the future is
easy to write and easy to ignore.

## 2. Who this is for

Unchanged from the generator set itself — the account layer doesn't add a
persona, it removes friction for the two that already exist.

- **The hobbyist maker.** Owns a printer, wants a customized mushroom or
  vase in five minutes. Never installing Python. Judges the product
  entirely on how fast slider → preview → STL feels on a phone.
- **The small-batch seller.** Runs an Etsy storefront, wants a dozen
  distinct pieces from one template. Cares that a signed-in account
  remembers what they've built, not just that one export worked.

## 3. Scope: shipped, simulated, planned

Three states, not two. *Simulated* is the important middle category this
prototype introduces — a feature that is visibly present and interactive,
but not backed by the real logic its production version needs.

| Capability | State | Detail |
|---|---|---|
| Sign up / log in | **Shipped** | Email + password against a real, persisted document store (Claude `db`). |
| Studio — design & preview | **Shipped** | The actual `web/studio.html`, all six templates, mounted verbatim. Not a re-creation. |
| STL / STEP / JSON export | **Shipped** | Real files, via the `downloads` capability, triangulated from the live slider state. |
| Dashboard, account page | **Shipped** | Reads the same user document written at signup. |
| Credits balance | **Simulated** | 3 credits granted at signup, displayed everywhere — never decremented. See §8. |
| "Build for real" (CAD-kernel STL) | **Simulated** | Studio's own built-in panel; correctly reports "no server behind this page" rather than pretending. The preview export above is the real deliverable today. |
| Payment / billing | **Planned** | No processor is wired up; nothing charges a card. See §11. |
| Password hashing | **Planned** | Stored in plaintext in a shared document store today. This is the sharpest gap in the spec — see §9. |
| Model history / re-download | **Planned** | Studio's exports go straight to the visitor's device; nothing is retained server-side to relist. |

## 4. Architecture

One HTML document, no server, no network calls to anything FormForge
operates. Three logical layers inside that one file:

```
Account shell  ──▶  Studio (verbatim)  ──▶  Claude capabilities
(home, signup,       web/studio.html,        db — JSON document store
 login, dashboard,    lazy-mounted on         downloads — hands the
 account)             first visit             visitor a file
```

**Why lazy-mount, not an iframe.** The Studio's script and stylesheet are
embedded (base64, decoded at runtime) rather than loaded in a nested
`<iframe>`. Two reasons: `window.claude` is not guaranteed to reach a
nested frame, which would silently break every export button; and the
Studio's own CSS resets (`body`, `:root` variables) are rescoped to a
`#studio-mount` container so they can share the top-level document with
the account shell's pages without one page's palette leaking into
another's. The Studio only mounts once the page holding it is actually
visible — its Three.js canvas needs a real, laid-out viewport the instant
its script runs, not a `display:none` container measuring zero.

**Why not a real backend yet.** Standing up Postgres, auth, Stripe and
object storage is real, multi-week infrastructure work. Shipping the
account experience as an Artifact first answers the actual open question —
does a signed-in Studio, reachable from a phone with no install step, hold
together as a product — before that investment is made.

## 5. Site map

| Route | Purpose |
|---|---|
| `/` (home) | Pitch, two CTAs: get started, sign in. |
| `/signup` | Email, password, confirm. Grants 3 credits, opens the dashboard. |
| `/login` | Email, password against the stored user document. |
| `/dashboard` | Credits balance, member-since, the single "Go to Studio" CTA. |
| `/studio` | The real Studio: template tabs, sliders, live 3D preview, export. |
| `/account` | Email, credits, join date. |

## 6. Core flow

1. **Sign up.** Email + password validated client-side, written to `users`, 3 credits granted.
2. **Land on the dashboard.** Balance and account age render from the same document just written.
3. **Open the Studio.** First visit only: styles and scripts decode and mount; every later visit just toggles visibility, so slider state survives navigating away and back.
4. **Design.** Pick a template tab, move sliders, watch the live preview — identical to the offline file.
5. **Export.** Download STL, STEP script, or parameters JSON. Delivered through `downloads`; nothing uploads anywhere.

## 7. Data model

One collection today. Everything the account shell reads or writes lives in
`users`:

```
// db.collection("users").doc(id)
{
  email:      string,
  password:   string,   // plaintext -- see §9
  credits:    number,   // starts at 3, not yet debited anywhere
  created_at: string    // ISO 8601
}
```

No `models` collection exists yet — exports are ephemeral, generated in the
browser at click time and never written back. Adding build history (§11,
Phase 2) means introducing one, keyed by user id, storing template + params
+ a timestamp, not the mesh itself.

## 8. Credits & pricing

What's live: every signup gets **3 credits**, shown on the dashboard and
account page. No action anywhere debits the balance — it is a promise the
product makes visually, not yet one it enforces. That's deliberate scope,
not an oversight: metering an export requires a decision the prototype
can't make on its own, made below so it isn't invented silently later.

| Tier | Price | Credits/mo | Gate |
|---|---|---|---|
| Free | $0 | 3, once | STL export from the live preview |
| Maker | $9/mo | 60 | + STEP, JSON, all six templates |
| Studio | $29/mo | 300 | + CAD-kernel "build for real" (§3), model history |

Figures are a starting proposal for Phase 2 (§11), not a measured price —
there is no compute-cost data yet because nothing metered has run.

## 9. Security & privacy — read before inviting real users

The `db` capability's default rule is permissive by design: every
signed-in viewer of the artifact can read and write every document in a
shared collection. That's the right default for a poll or a shared board.
For a `users` collection holding **plaintext passwords**, it means today,
any signed-in visitor to this artifact can query the entire user table.

> **This is the one item in this spec that should block wider distribution,
> not just production launch.** Two independent fixes, either sufficient on
> its own:
>
> 1. Scope credential documents under `data/users/{self}`, the path prefix
>    the `db` capability keeps private per-viewer even from the artifact's
>    own owner.
> 2. Never accept a password into this layer at all — authenticate through
>    Claude's own signed-in identity (the `user` capability) instead of a
>    hand-rolled email/password form, which also removes the
>    plaintext-storage question entirely.

Neither fix is in v1. Treat the current link as a working demo for people
who already trust each other, not as onboarding for strangers.

## 10. Known limitations

- No password reset, no email verification — a lost password is a lost account.
- No rate limiting on signup/login; the artifact has no concept of an IP or a bot.
- Every visitor to the artifact shares one database — there is no organization or tenant boundary beyond it.
- Credits are cosmetic (§8); nothing currently stops unlimited exports.
- Exports are the browser-side preview mesh, not the validated build123d/OCCT output — correctly disclosed by the Studio's own "Build for real" panel, but worth restating: what downloads today is not print-farm-grade dimensional guarantee, it's the same approximation `studio.html` has always shipped for zero-latency slider feedback.

## 11. Roadmap

**Phase 1 — harden the artifact (no new infra)**
- Move credentials under `data/users/{self}` or drop passwords for
  Claude-native identity (§9) — ships before any invite goes out beyond
  the team.
- Wire the credit balance to the export button so §8's promise is real,
  even at prototype scale.

**Phase 2 — connect the real backend**
- Point "Build for real" at FormForge's existing sandboxed API (accounts,
  billing, and the validation pipeline already exist in this repo) instead
  of showing the no-server message.
- Add the `models` collection (§7) for history and re-download.
- Real payment processor behind the tiers in §8.

**Phase 3 — graduate off the Artifact runtime**
- Once usage outgrows what `db`/`downloads` comfortably carry, move the
  account shell to a hosted page backed by the real API, keeping the
  Studio front end unchanged — it was never coupled to the artifact
  runtime in the first place.

## 12. What decides if this is working

- **Activation**: signups that reach the Studio and move at least one slider.
- **Export completion**: Studio visits that end in a successful download.
- **Return rate**: signed-in users who come back for a second session — the
  entire argument for accounts over the stateless offline file.
