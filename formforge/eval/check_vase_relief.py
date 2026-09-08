"""The acceptance test for the vase relief. Run as a module:

    python -m formforge.eval.check_vase_relief


Does the spline-lofted outer skin enclose what the polyline-lofted one does?
That is the failure this motif set actually has: a ruled loft between spline
sections cannot follow a ridge that never moves, and it fails *silently* --
handing back a watertight single solid with a tenth of itself missing. Comparing
the two lofts catches it in a few seconds, where a full DFM pass on a
137,000-triangle vase takes minutes.
"""

import itertools
import subprocess
import sys
import tempfile
from pathlib import Path

from formforge.registry import TemplateRegistry

RUN = '''
import re, sys
src = open(sys.argv[1]).read()
if sys.argv[2] == "1":
    src = re.sub(r"(?m)^SMOOTH = .*$", "SMOOTH = False", src)
g = {"__name__": "__main__"}
exec(compile(src.split("# --- 4. the cavity")[0], "s", "exec"), g)
print("%.1f %d %d" % (g["outer"].volume, g["POINTS"], g["SECTIONS"]))
'''

MOTIFS = ("vine", "laurel", "fern", "blossom", "rose", "lily", "damask", "trellis", "scale")
EXTREMES = [
    ("emboss_mm", 0.2), ("emboss_mm", 3.0),
    ("emboss_count", 1), ("emboss_count", 9),
    ("emboss_rows", 1), ("emboss_rows", 8),
    ("emboss_sharp", 0.0), ("emboss_sharp", 1.0),
    ("emboss_lo", 0.0), ("emboss_hi", 1.0),
    ("twist_deg", 360.0), ("twist_deg", -360.0),
    ("height_mm", 60.0), ("height_mm", 240.0),
    ("facets", 12), ("lobes", 12),
]
# What the *relief* is allowed to add, over the same vase with emboss=none. Not
# an absolute ceiling: an untwisted twelve-flute vase already loses six per cent
# here with no relief on it at all, which is a defect of the flutes and predates
# this motif set.
#
# 2.5 rather than something tighter, because a 2% shortfall on a 75 cm3 shell is
# 0.03 mm of wall spread over the whole surface -- a fraction of a layer. What
# this is guarding against is the skin collapsing somewhere, which is a
# ten-per-cent effect, not a rounding one.
CEILING = 2.5


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vase-relief-"))
    runner = work / "run.py"
    runner.write_text(RUN)
    template = TemplateRegistry.load(strict=True).get("vessel_vase")
    worst = 0.0
    bad = skipped = built = 0
    for motif, (name, value) in itertools.product(MOTIFS, EXTREMES):
        params = dict(template.defaults())
        params["emboss"] = motif
        params[name] = value
        if name == "lobes":
            params["lobe_mm"] = 2.0
        label = f"{motif}/{name}={value}"
        if template.validate_params(params):
            skipped += 1
            continue
        case = work / "case.py"
        case.write_text(template.render_source(params))
        out = []
        for poly in ("0", "1"):
            done = subprocess.run(
                [sys.executable, str(runner), str(case), poly],
                capture_output=True, text=True, timeout=600,
            )
            out.append(done.stdout.split())
        if len(out[0]) < 3 or len(out[1]) < 3:
            print(f"FAIL {label:28s} did not build", flush=True)
            bad += 1
            continue
        spline, polyline = float(out[0][0]), float(out[1][0])
        loss = 100 * (polyline - spline) / polyline
        # The same vase without the relief, so the number reported is what the
        # relief costs rather than what it inherited.
        plain = dict(params)
        plain["emboss"] = "none"
        bare = work / "plain.py"
        bare.write_text(template.render_source(plain))
        base = []
        for poly in ("0", "1"):
            done = subprocess.run(
                [sys.executable, str(runner), str(bare), poly],
                capture_output=True, text=True, timeout=600,
            )
            base.append(done.stdout.split())
        bs, bp = float(base[0][0]), float(base[1][0])
        loss -= 100 * (bp - bs) / bp
        worst = max(worst, loss)
        built += 1
        flag = "ok  " if loss <= CEILING else "FAIL"
        if loss > CEILING:
            bad += 1
        print(f"{flag} {label:28s} relief adds {loss:6.2f}%  P={out[0][1]} S={out[0][2]}", flush=True)
    print(f"\n{built} built, {skipped} rejected up front, worst {worst:.2f}%, "
          f"{bad} over the {CEILING}% ceiling")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
