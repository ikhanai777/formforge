# FormForge Studio SaaS — specification (v1)

**Status: prototype live, Phase 1 partially landed.** This describes a
working product, not a proposal — a hosted, account-gated front door onto
FormForge's six parametric generators, shipping today as a self-contained
Claude Artifact. Password hashing and real credit metering are live; the
per-account data isolation this spec originally called for is blocked on a
platform capability this account doesn't have — see §9.

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
| Credits balance | **Shipped** | 3 credits granted at signup; each STL/STEP/JSON export debits one, checked and persisted before the export runs. See §8. |
| "Build for real" (CAD-kernel STL) | **Simulated** | Studio's own built-in panel; correctly reports "no server behind this page" rather than pretending. The preview export above is the real deliverable today. |
| Payment / billing | **Planned** | No processor is wired up; nothing charges a card. See §11. |
| Password hashing | **Shipped** | SHA-256 client-side before the value ever reaches storage — see §9 for why this, and not the fix originally proposed, is what shipped. |
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
For a `users` collection, it means any signed-in visitor to this artifact
can query the entire user table — that part is unchanged by anything below
and is not fixable from inside this artifact.

**What v1 proposed, and what's actually available.** Both fixes originally
listed here — scoping credentials under `data/users/{self}`, or dropping
passwords for Claude's own signed-in identity — depend on the `user`
capability. Checked against this account's actual capability roster while
building Phase 1: **`user` is not in it.** Neither fix is implementable in
this environment, not a matter of which one to prefer.

**What shipped instead: password hashing.** Every password is run through
SHA-256 in the browser before it reaches `db` — signup stores the hash,
login hashes the entry and compares. This does not fix the underlying
exposure above: the `users` collection, hashes and all, is still readable
by any signed-in member with access to the artifact. What it removes is a
credential appearing anywhere in plaintext, which matters because the same
password reused elsewhere is a real, separate cost of that exposure beyond
this artifact's own data.

> **This artifact is still not where a stranger should set a password they
> use anywhere else.** Treat the current link as a working demo for people
> who already trust each other. The two fixes above remain the correct
> ones — they're blocked on platform capability, not on design — and
> should be revisited the moment `user` (or an equivalent identity
> primitive) is available to this account.

## 10. Known limitations

- No password reset, no email verification — a lost password is a lost account.
- No rate limiting on signup/login; the artifact has no concept of an IP or a bot.
- Every visitor to the artifact shares one database — there is no organization or tenant boundary beyond it.
- Credits are cosmetic (§8); nothing currently stops unlimited exports.
- Exports are the browser-side preview mesh, not the validated build123d/OCCT output — correctly disclosed by the Studio's own "Build for real" panel, but worth restating: what downloads today is not print-farm-grade dimensional guarantee, it's the same approximation `studio.html` has always shipped for zero-latency slider feedback.

## 11. Roadmap

**Phase 1 — harden the artifact (no new infra). Status: landed, partially.**
- ~~Move credentials under `data/users/{self}` or drop passwords for
  Claude-native identity~~ — **blocked**, not done: both depend on the
  `user` capability, which this account's roster doesn't include (§9).
  SHA-256 hashing shipped as the fix that's actually reachable from here;
  the per-account isolation problem is still open.
- **Done.** Credit balance is wired to the export buttons: each STL/STEP/JSON
  download checks the balance, refuses at zero, and debits one credit,
  persisted back to `db` and reflected live on the dashboard and account
  pages.

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
