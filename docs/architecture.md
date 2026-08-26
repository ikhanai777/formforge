# Architecture notes

The parts that need more than a paragraph in the README, and the places where
the implementation knowingly differs from the spec.

---

## 1. Where the measurements are approximate

Every geometric measurement in `formforge/validation/mesh.py` is an
approximation with a known failure mode. Writing them down is not humility, it
is operational: when a validator disagrees with reality, the first question is
which approximation broke, and that question is only answerable if the
approximations were stated.

### Wall thickness

Sample the surface, cast a ray inward along the normal, take the distance to the
first exit.

**Known biases, and which way they point.** At a concave corner the inward ray
travels diagonally and over-reports. It never under-reports, so a wall that
passes is genuinely thick enough — the error falls on the safe side, which is
the direction to want it in.

**Two corrections that are load-bearing:**

*Grazing exits are discarded.* A sample sitting on a sharp convex edge — the
side of an embossed letter, the seam where a flat back meets a curved wall —
casts its ray straight back out through the neighbouring face after almost no
distance, and reports a wall three microns thick. A ray crossing a real wall
exits through a face whose normal points along the ray; a ray grazing an edge
exits through one nearly perpendicular to it. Requiring the exit face to be
within 60° of facing the ray keeps the first and discards the second. Without
this, the template harness failed on essentially every part with text on it.

*Thresholds are compared against the 1st percentile plus a 0.02 mm allowance,
not the raw minimum.* The percentile is because a ray-cast thickness field has a
long thin tail wherever the surface curves sharply, and failing a whole model on
its single thinnest sample rejects good parts forever. The allowance is because
the mesh is tessellated at 0.05 mm and sampled finitely, so a wall modelled at
exactly 2.0 mm measures 1.9997 — and "exactly the minimum" is precisely what a
careful template author picks.

*On a model with raised lettering, the 5th percentile.* Text sidewalls are thin
by design, and on a small tag they are enough of the sampled surface to drag the
1st percentile below what the body actually measures. Text is already governed
by cap height, stroke width and relief depth in the legibility check; measuring
it a second time as thin walls rejected a printable name tag twice for the same
reason. The template declares that it renders text, so this is not a guess about
the geometry.

The same value is exposed to template invariants as `min_wall`, so a template's
own guarantee and the built-in check compare the same number. If they differed,
every template author would have to declare a bound lower than the rule they are
actually held to, and know why.

### Bridge span

For each downward-facing region above the plate: the distance from the point
furthest inside it to its nearest supported edge, doubled.

The obvious alternative — the region's overall width — is wrong in a way that
matters. A 2 mm lip running round the rim of a 120 mm pot is 120 mm across and
bridges trivially, because no point on it is more than a millimetre from solid
wall. Measuring width reported 55 mm and failed the planter template.

Sampling is at triangle centroids *and* edge midpoints. A flat annulus
triangulates into long triangles reaching from the inner ring to the outer one,
whose vertices all sit on the boundary and whose centroids land two-thirds of
the way across; centroids alone under-reported a 27 mm span as 18 mm.
Under-reporting a bridge is the dangerous direction — it passes a span that will
sag.

### Feature size

Derived from the thickness distribution rather than measured directly: a
protrusion thinner than the nozzle reads as a thin wall to an inward ray. The
approximation cannot tell a thin pin from a thin shell, so the check only fires
when the thin region is under 2% of the surface — which is what distinguishes an
isolated feature from a uniformly thin part.

### Hole diameter

Not approximated. Read from the B-rep's cylindrical faces, with internal versus
external decided by whether the surface normal points toward the axis. Fitting a
circle to a tessellated boundary would be fiddly and approximate; asking the
kernel takes microseconds and is exact. Only available on the parametric path —
the OpenSCAD path has no B-rep and falls back to mesh heuristics.

### Self-intersection

Uniform-grid broadphase over face AABBs, then an exact Möller triangle-triangle
test on the survivors, skipping pairs that share a vertex. Counting stops at 50:
the report only needs to know whether the mesh self-intersects and roughly how
badly, and a mesh with thousands is being regenerated regardless. Skipped
entirely above 120k faces, and *recorded as skipped* rather than silently passed.

### Text legibility

Not measured from geometry at all. The template declares which of its parameters
carry cap height, relief depth and stroke width, and those are checked. Recovering
stroke width from a tessellated glyph is possible and fragile; the numbers are
already known.

---

## 2. Deliberate departures from the spec

### Rendering: numpy instead of VTK

The spec calls for offscreen VTK on llvmpipe. These renders are 512×512
matte-grey previews read by a vision model, not marketing shots, and a numpy
rasteriser with a hand-written PNG encoder does that job with no native
dependency and no GL context to fail to acquire inside a container. It also
produces byte-identical output across machines, which matters more than it
sounds: the renders feed the visual critique, so a renderer that varies between
runs makes that step non-deterministic and its regressions impossible to bisect.

Measured at 72–97 ms per view against the spec's 600 ms budget. A prettier hero
renderer belongs behind the same interface, not in place of this one.

### Repair: the ladder is split, not ordered

The spec lists six rungs, cheapest first, and says to prefer regeneration over
repair on the parametric path. Both are right, but the ordering hides a real
distinction, so the ladder is split in two:

- **Lossless rungs** — vertex welding, dropping zero-area faces, winding fixes —
  provably do not move a single surface point, and run automatically. Without
  them, a perfectly good part fails Tier 1 over the two degenerate triangles
  that tessellating a sphere leaves at its poles.
- **Lossy rungs** — hole filling, manifold reconstruction, voxel remeshing —
  change the shape. They must be asked for explicitly and are refused on the
  parametric path entirely.

The reason for the hard line: a repaired mesh no longer matches its STEP file.
The user downloads a part that prints and a CAD file that does not match it, and
nothing in the system knows. A rejected model is a better outcome than that.

Whatever ran is recorded in the report, even when it provably moved nothing.

### Preconditions as a first-class concept

The spec's templates declare `invariants` — expressions checked after
generation. In practice about a third of the rules that want to be written are
not about the geometry at all; they are relationships between parameters ("the
text has to fit on the tag", "the fillet must be smaller than the arm").

Checking those afterwards produces a validation failure, which reads as "the
geometry is broken" when the truth is "those two numbers cannot both be right".
So templates declare both:

- `preconditions` — expressions over parameters, evaluated before building,
  reported as a parameter error.
- `invariants` — expressions over measured geometry, evaluated after.

Both are evaluated by a restricted AST walker, never `eval`, so a registry entry
cannot become an execution vector.

### Routing knows when a request carries a literal

Similarity scoring cannot route `a keychain that says "RIVER"`. The literal
itself matches no template's vocabulary, so the query shares exactly one
scoring token — "keychain" — with both keychain templates, they tie, and the
winner is whichever loaded first. Half the time that is the bottle opener,
which has nowhere to put the word: it would build a plausible part and silently
drop the one thing the user actually asked for.

So routing takes a second input: whether the parsed intent carries text to
render. Templates that declare no text parameter are scaled down when it does.
A penalty rather than a boost for the ones that can, because the claim is that
those candidates are *wrong*, not that the others are more similar — and
scaling down can never lift a weak match over a threshold it had not already
cleared. Ties are then broken by template id, so the same query always routes
the same way.

### A template's vocabulary is a claim on every other template's queries

The lexical scorer ranks a template by how much of the *query's* IDF weight its
token set covers. Nothing normalises for how much the template says, so a
template with a generous vocabulary quietly outranks a narrow one on queries
that belong to the narrow one. Adding the rippled planter demonstrated it: the
draft described itself as a "tall floor planter" whose "wall is even
everywhere", and those two words took `a wall planter 130mm wide and 100mm tall`
away from the wall planter and `something to keep pens and scissors on my desk,
about 9cm tall` away from the pen cup. Neither template changed; both lost.

The rule that falls out is that a template's searchable text may carry the words
that identify *it* and not the words that merely describe the shape of a
request. "Tall", "wide", "small", "desk" are query furniture; "rippled",
"gridfinity", "keyhole" are identity. Dimension words belong in the parameter
descriptions, which are not searched.

This is a property of the lexical default, not of the product: an embedding
matcher scores direction rather than coverage and does not have this failure in
the same form. It is worth writing down anyway, because the offline path is what
the benchmark and every test run against, so a template that behaves badly here
behaves badly everywhere they can see.

### When two values conflict, the default yields

A stated dimension and an untouched default can be individually valid and
jointly forbidden. A 40 mm hex tile is within the schema; so is the 10 mm
default border; together they violate the template's own
`border_w_mm * 2 + 20 < across_flats_mm`.

The first version resolved this by dropping values until what remained
validated — and dropped the 40 mm, because it was the value present in the
conflict. The user asked for a 40 mm tile and got a 100 mm one, with the reason
buried in a note. That is the wrong half of the pair to lose: the border width
is a knob nobody mentioned.

So before dropping anything, the parameters the failing rules mention *that the
caller did not choose* are tried at either end of their declared range, keeping
any move that leaves strictly fewer problems, for at most three rounds. It is
deliberately not a solver — two candidate values per parameter, one parameter
per round, in schema order — because the result has to be describable in a
sentence, and it is: "border_w_mm=6, hang_d_mm=0". A template that needs more
than that to produce any valid combination has a schema problem, and §6 is what
should be reporting it.

Whatever moved is named in the result. A silent adjustment is the same bug
wearing a different hat.

### The offline path is real

`OfflineClient` is not a stub. With no API key the template path runs end to
end: the registry's lexical matcher picks a template, regexes pull dimensions,
units and quoted text out of the prompt, and schema defaults fill the rest.

This is worth building because the template path is where most traffic should go
anyway (it is both the reliability story and the primary cost lever), so the
system degrades to "templates only" rather than to "broken" — and because it
makes the whole loop testable in CI without credentials.

---

### Collection is on by default

`generation_events` and `print_feedback` are the two tables that cannot be
backfilled — the first is how a four-iteration run becomes explicable after the
fact, the second is the only ground truth that exists for whether any of this
prints — so both are written from the CLI and the API alike, on someone's laptop
as well as in a deployment. Early on the laptop is where the runs are.

Two rules follow from what each table is for:

*Telemetry writes never raise into the caller.* Losing a row is a bad day;
failing a generation the user waited thirty seconds for because an event could
not be logged is worse. Failures are counted, and `write_failures` is what a
health check reads.

*Feedback writes do raise.* That caller is a person telling us what happened to
their print, and dropping it silently is worse than a 500 they can retry. It is
also refused outright when it names a model that was never recorded: feedback
that cannot be joined to what the validator measured answers no question.

The `issues` vocabulary is closed for the same reason. "Warped a bit on one
corner" and "warping" have to be the same row before any of this can tune a DFM
constant, and each term names a failure that some check already measures — so
`weak` lands next to the `min_wall_mm` recorded at the time.

## 3. Cost and caching

The freeform path is five to ten times the template path, so **template coverage
is the primary cost lever**. Every template added moves traffic off the expensive
route. That is a second reason to invest in the registry, on top of reliability.

Within the freeform path, the input cost is dominated by a stable prefix: the
DFM rules, the build123d cheat-sheet, and the registry summary come to roughly
8–12k tokens that are identical for every request with the same printer and
material. Cached, they cost a tenth as much.

The prefix must be **byte-identical** between calls. Everything that varies goes
after the last cache breakpoint. Specific things that would silently break it,
all of which are guarded against in the code:

- a timestamp or request id anywhere in the system prompt
- unsorted `dict` iteration when building the registry summary
- a float that formats as `2.4000000000000004` on one run and `2.4` on another
- editing the cheat-sheet, which invalidates every request until it warms again

`Usage.cache_hit_rate` exists to catch this. A rate near zero means something is
invalidating the prefix and the codegen path is costing several times what it
should.

Model tiering: template fills go to Haiku (a schema fill, not a reasoning
problem), freeform to Sonnet, and a script that has failed three times escalates
the whole conversation to Opus for **one** attempt. Escalating once rather than
retrying forever is what bounds a pathological request.

---

## 4. Deployment

```
gateway + orchestrator     stateless, horizontally autoscaled
geometry workers           the expensive, dangerous tier:
                             gVisor runtime, dedicated node pool,
                             2 vCPU / 4 GB, one job at a time,
                             autoscaled on queue depth
render workers             can share the geometry pool; software
                             rasterisation, no GPU
```

Set in the geometry tier:

```
FORMFORGE_SANDBOX_RUNTIME=gvisor
FORMFORGE_SANDBOX_IMAGE=formforge/geometry:<digest>
FORMFORGE_SECCOMP_PROFILE=/etc/formforge/seccomp.json
```

The sandbox image should carry only build123d, numpy and trimesh — not the API
layer, not the model client, not the registry. The orchestrator passes source
and parameters, and nothing else; no credential should be reachable from the
tier that executes generated code.

`GeometrySandbox.production_ready()` reports whether the active runtime isolates
the host kernel, and `create_app` refuses to start when it does not. Override
only for local development.

The database belongs to the orchestrator tier and nothing else. `FORMFORGE_DB`
points at it (default `~/.formforge/formforge.db`); the API opens one beside its
artifact store. The geometry tier must not reach it — that tier executes
generated code, and the whole point of giving it no credentials is that there is
nothing there to take.

---

## 5. What is not built

Stated plainly, so nothing here reads as more finished than it is.

- **The hybrid decorative path** (SDF fields, image-to-heightmap relief,
  mesh-generation detail booleaned onto a parametric base). The rule it must
  follow is settled — functional geometry is never generated, and the
  post-boolean result is re-validated against the same DFM rules — but the path
  itself is not implemented.
- **OpenSCAD execution.** The adapter is written and reports cleanly when the
  binary is absent; it has not been exercised.
- **Postgres.** The data model is in `docs/schema.sql` and `formforge/store.py`
  implements it on SQLite — same tables, same column names, same two views. The
  Postgres-only parts are absent rather than faked: the `vector(1536)` embedding
  column and its ivfflat index, `citext`, and the `users` table that auth would
  need. Moving over is a dialect change; §2 has the reasoning for starting on
  the file-backed one, which is that a persistence layer needing a running
  database is one that gets switched off in development, and a table empty for
  six months is worth nothing.
- **Auth, quotas, billing.** The API has no authentication.
- **Physical print testing.** Every template carries a `tested` block, and those
  blocks are *unverified* — no model in this repository has been printed. Spec
  section 13.3 is right that thirty physical prints before launch is the highest
  value item in the whole plan, and it is the one thing no amount of validation
  substitutes for. The DFM constants are conventional maker values, not
  empirical ones, until then. What *is* built is the path from a print to a
  constant: `formforge feedback`, `POST /v1/feedback` and the MCP
  `report_print_result` tool all land a row in `print_feedback`, and
  `print_outcomes` reads it back beside the wall thickness measured at the
  time. `formforge doctor` says how many rows are there, which is currently
  zero and should be visible.

---

## 6. What the range sweep is for

`python -m formforge.eval.check_templates --extremes` builds every template at
its defaults *and* at each parameter's declared minimum and maximum, one
parameter at a time. The premise is simple: a schema that permits a 200 mm
planter is a promise that a 200 mm planter builds, and nothing else in the
system holds the registry to that promise.

It has earned its runtime. Findings that no unit test would have produced:

| What broke | Why nothing else caught it |
|---|---|
| Thickness rays grazing a sharp convex edge reported 3 µm walls | Only appears on parts with text or a flat-meets-curve seam |
| A 2 mm rim lip round a 120 mm pot read as a 55 mm unbridgeable span | Needed a real annular ceiling to expose the metric's flaw |
| The planter's hanger tab came out as a second disconnected solid | Coplanar contact looks like a union until you count the bodies |
| The honeycomb lattice cut its own panel into seven pieces | Only at the minimum rib width, where the overlap finally bit |
| A "120 mm" hexagonal sign came out 139 mm across | The default shape is a circle, where the bug does not exist |
| Fillets larger than the floor they sit on | Only at the minimum floor thickness |

The pattern is consistent: **defaults are the configuration that gets tested by
hand, so bugs accumulate at the edges of the ranges.** Sweeping one parameter at
a time rather than all at once keeps each failure attributable to a single
value; sweeping combinations would find more, at the cost of reports nobody can
act on.

Combinations a template already declares invalid are reported as skipped rather
than failed. Driving one parameter to an extreme routinely produces a
combination its preconditions forbid, and counting those as failures would bury
the cases where the geometry genuinely breaks.
