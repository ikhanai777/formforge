# Image to key holder

```bash
formforge keyholder cat.png --width 200 --hooks 5
```

A picture goes in; a wall-mounted key rack comes out, as a bundle with the same
STL, 3MF, STEP, `source.py`, `params.json`, `report.json` and previews every
other FormForge model ships with.

This is the one path in the system where a user supplies *shape* rather than
words, so it is worth being precise about what the image is allowed to decide
and what it is not.

**The image decides the silhouette. Nothing else.** The hook rail, the hook
profile, the keyhole hangers, the minimum feature size, the print orientation
and every dimension in the part are generated parametrically and checked against
the same DFM rules as any template. An uploaded photograph never becomes
geometry directly; it becomes a polygon, the polygon is cleaned in the plane
until it is manufacturable, and only then does a CAD kernel see it.

That division is what keeps the claim in the README true for this path too: the
model is exact, watertight by construction, dimensioned, editable afterwards,
and exportable to STEP. An image-to-mesh generator asked for the same thing
produces a shell with no wall thickness, no fixings and no flat back.

## The stages

```
image.py    pixels   -> foreground mask     which pixels are the subject
trace.py    mask     -> polygon + holes     marching squares, then Shapely
design.py   polygon  -> layout in mm        the product decisions
emit.py     layout   -> build123d script    standalone, named constants
pipeline.py script   -> sandbox, validation, renders, bundle
```

### Deciding what the subject is

Three rules, in order:

1. **A meaningful alpha channel wins.** A cut-out PNG has already answered the
   question, and nothing else is consulted. This is the case that traces
   perfectly, and it is worth telling users so.
2. **Otherwise, distance from the border colour.** The background of a product
   photo is whatever the edge of the frame is — a green screen, a white studio
   sweep, a beige wall. Each pixel's colour distance from the median border
   colour is thresholded with Otsu. A plain luminance threshold handles only the
   white-background case: a pale wooden car on a green screen is *brighter* than
   its background and a black van on a beige wall is darker, and colour distance
   gets both right.
3. **Then connectivity decides the rest.** The subject is the largest connected
   blob. Regions of background fully enclosed by it — the windows of a van, the
   counter of an "o" — are openings in the subject rather than background, and
   are classified by where they are rather than by what colour they are.

Every decision is reported back in the result (`image.notes`), because "it
traced the wrong thing" is the failure that matters and the user needs to know
which assumption produced it. `--threshold` moves the cut and `--invert` flips
the sense when the subject is what touches the frame.

Photographs vary. Clip art, logos, cut-outs and silhouettes are reliable.
`--plan-only` traces, lays out and draws the result in about a second without
starting the CAD kernel, and it is the right first call on any photograph.

### Making the outline manufacturable

The trace is a few hundred points describing something drawn by a camera, not by
an engineer. Three passes in Shapely, in this order, turn it into a shape a
printer can make:

| Pass | What it does | Why it is in this order |
|---|---|---|
| Closing (`--close-gaps`, 0.8 mm) | Merges parts separated by a hairline | Runs first so the opening does not delete a limb that was about to be reattached |
| Border (`--border`, 0 mm) | Grows the whole silhouette outwards | Optional; turns a spindly trace into a plate with an edge |
| Opening (`--min-feature`, 2.4 mm) | Deletes necks and spikes thinner than the minimum feature | The step that turns "the validator says min wall 0.7 mm" into a part that prints |

The opening is a genuine guarantee, not a heuristic: after
`buffer(-r).buffer(+r)` at `r = min_feature / 2`, nothing in the plane is
thinner than `min_feature`. What it costs is reported (`removed features thinner
than 2.4 mm (6% of the traced area)`), because deleting part of someone's cat is
not something to do quietly.

Anything that survives but no longer touches the hook rail is dropped, with a
warning naming the fraction lost: a decorative fragment floating 3 mm from the
plate is a loose piece, not a feature. `--rail-overlap` and `--border` are the
two knobs that reattach it.

### Print orientation, and what it costs

**The part is modelled lying on its back, which is how it prints.** For an
arbitrary silhouette this is not a preference:

- Flat on the plate, every edge of the outline is a *vertical wall*. No overhang,
  no supports, no stringing across a curve nobody chose.
- Standing on edge, every one of those curves is an overhang whose angle came
  from a photograph, and half of them are unprintable.

The cost is real and is stated in the header of every generated `source.py`: the
layer lines run parallel to the wall, so a hook root is loaded *across* the
layers rather than along them. That is the weak direction in FDM. Three things
answer it — the hooks are wedges rather than pegs, their 45° underside is a
gusset into the plate, and the note recommends PETG with four perimeters for a
rack that will carry heavy bunches.

### The hook

Drawn in the side view as `(out-from-wall, up-the-wall)` and extruded across.
Because the part prints flat, "out from wall" is the build direction, which
makes every rising edge of this profile an overhang and every horizontal one a
vertical wall. So the profile obeys two rules:

- the lip rises at exactly 45°, the steepest self-supporting angle;
- the underside falls at 45° or shallower.

```
                        ,--.  <- lip tip, 45 deg front face
   plate               /    |
   |          _______,'     |
   |         |              |     top surface: where the ring rests
   |         |             /
   |         '-.         ,'      underside: 45 deg, and the gusset
   |            '-.____,'
   +--- z (out from wall, and the build direction) --->
```

Nothing in a hook needs support material, which matters more here than anywhere
else in the part: supports inside a hook cannot be reached with pliers.

### Hanging it on the wall

The default fixing is a keyhole hanger cut into the back, which is a sandwich of
three thicknesses:

| Layer | Default | What it does |
|---|---|---|
| Retaining lip | 2.4 mm | The material against the wall that the screw head pulls against |
| Head cavity | 3.0 mm | Room for the head along the whole of its travel |
| Front wall | ≥ 1.2 mm | So the head does not print through the visible face |

They have to add up, which is why the plate defaults to 7 mm and why a thinner
plate is either thickened (with a note) or refused in favour of `--mount screw`.
A per-parameter range cannot express a relationship between three numbers, so it
is checked before anything is built — the same reason templates have
`preconditions`.

Two fixings are placed at *the same height*, as high up the piece as they fit
with 2.5 mm of material all round. Same height, because two screws at different
heights hang the piece crooked no matter how carefully they are drilled; as high
as possible, because a piece hung near its top lies flat against the wall where
one hung near its bottom pivots off it when the hooks are loaded. Finding them
is one erosion of the plate polygon and a scan of segment containments — the
keyhole outline is exactly a disc swept along its slot, so "does it fit with a
margin" is "is the slot's centre line inside the plate eroded by the head radius
plus that margin".

If the silhouette has no region solid enough, the search falls back to the hook
rail, which this module drew itself and can guarantee. If even that fails, the
piece is built without a fixing and says so.

## Validation

Key holders validate as their own category, `key_holder`, with tier-3
invariants alongside the universal topology and printability tiers:

- `key_holder.one_piece` — one solid, or a fragment is about to print loose.
- `key_holder.hook_root` — nothing under 2.4 mm, *except* on a keyhole-mounted
  piece. The exception is deliberate and is the interesting part: the cover in
  front of a keyhole cavity is designed to be thin. It is a floor, not a wall —
  it carries no load and only has to be opaque — so the general rule would
  condemn the one wall in the design that is meant to be under 2.4 mm.
  Everything structural is guaranteed by construction instead (the outline is
  opened to the minimum feature size before it is built), and
  `key_holder.cover` still fails anything under 1.2 mm.
- `key_holder.mount_present`, `key_holder.adhesive_mass`,
  `key_holder.plate_flat` — warnings for a piece with no fixing, one too heavy
  for adhesive, and one that is not lying on its back.

## The repair loop

The generic loop asks a model to fix its own code. This one does not need to:
every failure a traced silhouette produces has a known cause and a known
adjustment.

| Failure | Adjustment |
|---|---|
| `key_holder.hook_root`, `printability.min_wall`, `min_feature` | Raise the minimum feature size and re-open the outline |
| `key_holder.one_piece`, `topology.solid_count`, `stray_shards` | Close larger gaps, push the rail further into the silhouette |
| The kernel refused the profile, or the mesh came back unsound | Simplify harder — fewer, cleaner vertices |
| Anything else | Stop, and report the validator's own words |

Three attempts, then it stops rather than guessing a fourth time. The whole path
runs offline at zero token cost, and the same image with the same parameters
produces the same STL every time.

## Interfaces

```bash
formforge keyholder cat.png --plan-only          # trace, draw, stop (seconds)
formforge keyholder cat.png --width 200 --hooks 5
formforge keyholder photo.jpg --threshold 0.35 --invert
formforge keyholder logo.png --detail cut --mount screw --plaque-t 4
```

- **Browser**: `formforge serve --allow-unsafe-sandbox`, then
  <http://127.0.0.1:8000/>. Described below.
- **MCP**: `generate_key_holder`, with `plan_only` for the cheap first pass. The
  outline drawing comes back inline as an image, so the model can look at what
  it traced and change the threshold itself.
- **HTTP**: `POST /v1/keyholder` with the image base64 encoded. Same job
  machinery, same WebSocket event stream and same bundle as `/v1/generate`.
  `POST /v1/keyholder/plan` is the synchronous half: trace, lay out, return the
  drawing, no kernel.
- **Python**: `plan_from_image(...)` for the layout alone, `build_key_holder(...)`
  for the whole thing.

### The browser interface

`formforge/api/static/` — three files, no build step, no CDN. The offline path
is a product feature everywhere else in this system, and a UI that needed a
network to render its own stylesheet would be the one place it stopped being
true.

The page is built around the same split as the CLI's `--plan-only`, and that
split is the whole design:

| | plan | build |
|---|---|---|
| Endpoint | `POST /v1/keyholder/plan` | `POST /v1/keyholder` |
| Costs | about a second, no kernel | about a minute of OCCT |
| Runs when | any control moves | the button is pressed |
| Answers | *did it see the right shape?* | *is it printable?* |

So every slider repaints the outline, and the geometry runs only once someone
has looked at it. The drawing is on screen the whole time, the plan's notes and
warnings are listed under it verbatim, and the build streams the same phase log
the CLI prints over the WebSocket the API already had.

Three smaller decisions in there:

* **Images are sent as they are when they are small.** Re-encoding a cut-out PNG
  in a canvas would throw away its alpha channel, which is the one input that
  traces perfectly. Only a photograph over 4 MB is resized in the browser first.
* **A preview name is looked up, never joined onto a path.**
  `/v1/models/{id}/previews/{name}` resolves against that model's own preview
  map, so a name that arrives in a URL never reaches the filesystem.
* **The job goes terminal only once its bundle is written.** The WebSocket's
  "closed" frame is what the page waits on, and flipping the status before
  `source.py` exists hands a client a model whose download links are still
  appearing.

`formforge serve` binds to loopback. `--allow-unsafe-sandbox` is how a user says
they accept running generated Python on a runtime that does not isolate the host
kernel, and it is refused outright on any non-loopback address: "I accept the
risk on my own laptop" and "I accept it for the internet" are different
sentences, and the environment variable alone cannot tell them apart.

Every bundle from this path carries one extra preview, `preview_trace.png`: the
outline that survived cleanup, the openings that became engraving, and where the
hooks and fixings landed. When a key holder comes out wrong the reason is almost
always visible there and almost never visible in a render of the result.

## Where it is weak

- **Photographs with soft or busy backgrounds.** Colour distance is a good
  heuristic and not a segmentation model. A subject that shades into its
  background traces into a blob with ragged edges. The drawing shows this
  immediately, which is the mitigation; `--threshold` is the fix.
- **Fine interior detail.** Engraving is 1 mm deep and as wide as the trace found
  it, so lettering below a few millimetres tall comes out mushy. The DFM text
  checks do not apply here, because there is no text feature to declare — the
  detail is just polygons.
- **Nothing here has been printed.** Every threshold in this path is a
  conventional maker value, exactly as everywhere else in this repository. The
  45° self-supporting rule, the 2.4 mm minimum feature and the keyhole
  proportions are all conventions until `formforge feedback` says otherwise.
