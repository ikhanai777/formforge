# Slicer profiles

Drop PrusaSlicer or OrcaSlicer `.ini` files here, named to match the
`slicer_profile` field on each entry in `formforge/dfm.py`:

```
generic_fdm_0.4_pla_0.20.ini
generic_fdm_0.6_pla_0.30.ini
bambu_p1s_pla_0.20.ini
prusa_mk4_pla_0.20.ini
ender3_v3_pla_0.20.ini
generic_fdm_0.4_pla_0.30.ini
```

**None ship with this repository, deliberately.** A slicer profile is a few
hundred tuned values, and an untested one produces print-time and filament
estimates that look authoritative and are wrong — the same failure mode as
claiming a design has been print-tested when it has not. Export the profiles
from your own slicer, where they came from a machine that exists.

Without them, `formforge.slicer` still works: it drives the CLI with the nozzle
diameter and layer height from the printer profile instead. The estimates are
coarser and the support-volume ratio is still meaningful, since it compares two
runs made the same way.

`formforge doctor` reports whether a slicer binary was found at all.
