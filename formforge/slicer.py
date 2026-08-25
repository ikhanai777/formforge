"""Slicer integration (spec section 6.5).

Wraps the PrusaSlicer/OrcaSlicer CLI to get real print time, filament use and
support volume rather than estimates. Two things make this worth the
integration:

* **Print time and filament are the numbers users actually decide on.** "About
  four hours and 32 grams" changes whether someone hits print; "48210 triangles"
  does not.
* **Support volume is a design signal, not just a statistic.** Slicing twice --
  once with supports, once without -- gives a ratio that says whether the part
  is well oriented. A wall planter that needs 40% support volume is a badly
  designed or badly oriented planter, and the agent should be told so and given
  a chance to fix it (spec section 6.5).

The slicer is optional. It is a large native binary that may not be present, so
every entry point reports unavailability as data rather than raising -- a
missing slicer must degrade the bundle, never fail the generation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .dfm import get_profile

PROFILE_DIR = Path(__file__).parent.parent / "profiles"

# Binaries to look for, in preference order. OrcaSlicer shares PrusaSlicer's CLI
# lineage, so the same arguments work.
SLICER_BINARIES = ("prusa-slicer", "prusaslicer", "PrusaSlicer", "orca-slicer", "orcaslicer")

# Slicing a complex part is slow, and a hung slicer must not hold a request open.
SLICE_TIMEOUT_S = 180

# Above this fraction of support-to-part volume, the part is badly oriented
# rather than merely support-requiring.
SUPPORT_RATIO_WARN = 0.05
SUPPORT_RATIO_FAIL = 0.30


@dataclass
class SliceSummary:
    """What a slicer says about a model."""

    available: bool = False
    ok: bool = False
    print_time_s: int | None = None
    filament_mm: float | None = None
    filament_g: float | None = None
    layer_count: int | None = None
    support_ratio: float | None = None
    gcode_path: str | None = None
    profile: str = ""
    quality: str = "standard"
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def print_time_human(self) -> str:
        if not self.print_time_s:
            return "unknown"
        hours, remainder = divmod(int(self.print_time_s), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "ok": self.ok,
            "print_time_s": self.print_time_s,
            "print_time": self.print_time_human,
            "filament_mm": self.filament_mm,
            "filament_g": self.filament_g,
            "layer_count": self.layer_count,
            "support_ratio": self.support_ratio,
            "profile": self.profile,
            "quality": self.quality,
            "error": self.error,
            "warnings": self.warnings,
        }

    def agent_feedback(self) -> str:
        """The slice result as loop feedback, when it says something actionable."""
        if not self.ok:
            return ""
        notes: list[str] = []
        if self.support_ratio is not None and self.support_ratio > SUPPORT_RATIO_FAIL:
            notes.append(
                f"This part needs {self.support_ratio * 100:.0f}% of its own volume "
                "again in support material. That is a design or orientation "
                "problem, not a printing one: reorient it so the large flat face "
                "is on the bed, or chamfer the overhanging faces to 45 degrees."
            )
        elif self.support_ratio is not None and self.support_ratio > SUPPORT_RATIO_WARN:
            notes.append(
                f"Needs {self.support_ratio * 100:.0f}% support volume. Acceptable, "
                "but a support-free version would print faster and cleaner."
            )
        return "\n".join(notes)


def find_slicer() -> str | None:
    """Locate a slicer binary, or None."""
    for name in SLICER_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


def available() -> bool:
    return find_slicer() is not None


def profile_path(profile_id: str) -> Path | None:
    """The slicer .ini for a printer profile, if one is shipped."""
    profile = get_profile(profile_id)
    if not profile.slicer_profile:
        return None
    candidate = PROFILE_DIR / profile.slicer_profile
    return candidate if candidate.exists() else None


def slice_model(
    model_path: str | Path,
    *,
    profile_id: str = "generic_fdm_0.4",
    quality: str = "standard",
    supports: bool = False,
    measure_support_ratio: bool = True,
    out_dir: str | Path | None = None,
) -> SliceSummary:
    """Slice a model and parse the resulting G-code metadata."""
    binary = find_slicer()
    summary = SliceSummary(profile=profile_id, quality=quality)
    if not binary:
        summary.error = (
            "no slicer binary found on this host (looked for "
            + ", ".join(SLICER_BINARIES)
            + "); print-time and filament estimates are unavailable"
        )
        return summary

    summary.available = True
    source = Path(model_path)
    if not source.exists():
        summary.error = f"{source} does not exist"
        return summary

    workdir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="formforge-slice-"))
    workdir.mkdir(parents=True, exist_ok=True)

    primary = _run_slice(binary, source, workdir / "out.gcode", profile_id, quality, supports)
    if not primary.ok:
        return primary

    summary = primary
    summary.profile = profile_id
    summary.quality = quality

    # Slice again with supports flipped to get the ratio. Only worth doing when
    # the first pass was support-free: if supports were already on, the filament
    # figure already includes them and a second run tells us nothing new.
    if measure_support_ratio and not supports:
        with_supports = _run_slice(
            binary, source, workdir / "out_supported.gcode", profile_id, quality, True
        )
        if with_supports.ok and primary.filament_mm and with_supports.filament_mm:
            extra = max(0.0, with_supports.filament_mm - primary.filament_mm)
            summary.support_ratio = round(extra / primary.filament_mm, 4)

    return summary


def _run_slice(
    binary: str,
    source: Path,
    output: Path,
    profile_id: str,
    quality: str,
    supports: bool,
) -> SliceSummary:
    summary = SliceSummary(available=True, profile=profile_id, quality=quality)

    cmd = [binary, "--export-gcode", "--output", str(output)]
    ini = profile_path(profile_id)
    if ini:
        cmd += ["--load", str(ini)]
    else:
        # Without a profile, drive the essentials from the printer definition so
        # the numbers still reflect the machine the user chose.
        profile = get_profile(profile_id)
        layer = {"draft": profile.layer_mm * 1.5, "fine": profile.layer_mm * 0.6}.get(
            quality, profile.layer_mm
        )
        cmd += [
            f"--nozzle-diameter={profile.nozzle_mm}",
            f"--layer-height={round(layer, 2)}",
            f"--first-layer-height={round(profile.layer_mm, 2)}",
        ]
    cmd.append("--support-material" if supports else "--support-material=0")
    cmd.append(str(source))

    try:
        proc = subprocess.run(  # noqa: S603 -- argv built here, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=SLICE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        summary.error = f"the slicer did not finish within {SLICE_TIMEOUT_S}s"
        return summary
    except OSError as exc:
        summary.error = f"could not run the slicer: {exc}"
        return summary

    if proc.returncode != 0 or not output.exists():
        summary.error = (proc.stderr or proc.stdout or "the slicer failed").strip()[:1000]
        return summary

    parsed = parse_gcode_metadata(output)
    summary.ok = True
    summary.gcode_path = str(output)
    summary.print_time_s = parsed.get("print_time_s")
    summary.filament_mm = parsed.get("filament_mm")
    summary.filament_g = parsed.get("filament_g")
    summary.layer_count = parsed.get("layer_count")
    return summary


# G-code trailer comments, as PrusaSlicer and OrcaSlicer write them.
_TIME_RE = re.compile(r";\s*estimated printing time.*?=\s*(.+)", re.IGNORECASE)
_FILAMENT_MM_RE = re.compile(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", re.IGNORECASE)
_FILAMENT_G_RE = re.compile(r";\s*filament used \[g\]\s*=\s*([\d.]+)", re.IGNORECASE)
_LAYER_RE = re.compile(r";\s*total layer(?: number|s? count)\s*[:=]\s*(\d+)", re.IGNORECASE)
_DURATION_RE = re.compile(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?")


def parse_gcode_metadata(path: str | Path) -> dict:
    """Read the estimate comments out of a G-code file.

    Only the tail is read. These comments are written at the end of the file,
    and a G-code file for a large part is tens of megabytes -- reading all of it
    to find four lines would make slicing look far slower than it is.
    """
    file = Path(path)
    try:
        with file.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return {}

    result: dict = {}
    time_match = _TIME_RE.search(tail)
    if time_match:
        result["print_time_s"] = _parse_duration(time_match.group(1).strip())
    for key, pattern, cast in (
        ("filament_mm", _FILAMENT_MM_RE, float),
        ("filament_g", _FILAMENT_G_RE, float),
        ("layer_count", _LAYER_RE, int),
    ):
        match = pattern.search(tail)
        if match:
            result[key] = cast(match.group(1))
    return result


def _parse_duration(text: str) -> int | None:
    """Parse '2h 14m 3s' into seconds."""
    match = _DURATION_RE.search(text)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total or None
