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
previews/       front, top, isometric, a section cut, and a contact sheet
```

Those four views are the ones the critique step looks at, which is why they
are the ones a bundle carries. `formforge render` will produce any of the
eight — the six orthographic faces, the isometric and the section — from an
STL afterwards.

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
