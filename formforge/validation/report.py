"""The validation report: the structured object the whole loop is steered by.

Two audiences, one document. The agent loop reads `hard_failures` to decide
whether to repair and feeds `agent_feedback()` back into the prompt; the user
reads the same report rendered in the UI. Keeping them the same object means the
explanation a user is given is exactly the evidence the system acted on.

Every check carries its measured value and the threshold it was compared
against. "Wall too thin" is not actionable; "min wall 1.08 mm at (12.4, -3.1,
6.0), needs 1.2 mm" tells a model precisely what to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How a failed check affects the loop.

    FAIL   blocks delivery and triggers a repair iteration.
    WARN   is surfaced to the user but does not block; the model may address it
           opportunistically if it is already regenerating.
    INFO   is a measurement, recorded for the print-feedback dataset.
    """

    FAIL = "fail"
    WARN = "warn"
    INFO = "info"
    PASS = "pass"


@dataclass
class Check:
    """One measurement against one threshold."""

    id: str
    tier: int
    title: str
    severity: Severity
    passed: bool
    message: str
    measured: Any = None
    threshold: Any = None
    unit: str | None = None
    # Where on the model the problem is, when a location is meaningful.
    location_mm: list[float] | None = None
    # What to do about it, aimed at the model rather than the user.
    remedy: str | None = None

    @property
    def is_hard_failure(self) -> bool:
        return not self.passed and self.severity is Severity.FAIL

    @property
    def is_warning(self) -> bool:
        return not self.passed and self.severity is Severity.WARN

    def as_dict(self) -> dict:
        payload: dict[str, Any] = {
            "id": self.id,
            "tier": self.tier,
            "title": self.title,
            "severity": self.severity.value,
            "passed": self.passed,
            "message": self.message,
        }
        for key, value in (
            ("measured", self.measured),
            ("threshold", self.threshold),
            ("unit", self.unit),
            ("location_mm", self.location_mm),
            ("remedy", self.remedy),
        ):
            if value is not None:
                payload[key] = value
        return payload

    def describe(self) -> str:
        parts = [self.message]
        if self.measured is not None and self.threshold is not None:
            unit = f" {self.unit}" if self.unit else ""
            parts.append(f"(measured {_fmt(self.measured)}{unit}, limit {_fmt(self.threshold)}{unit})")
        if self.location_mm:
            parts.append(f"at ({', '.join(f'{v:.1f}' for v in self.location_mm)})")
        if self.remedy:
            parts.append(f"-> {self.remedy}")
        return " ".join(parts)


@dataclass
class ValidationReport:
    """The full DFM report for one generation (spec section 7)."""

    checks: list[Check] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    profile_id: str = ""
    material: str = "PLA"
    # Set when a tier could not run at all -- a mesh that would not load, a
    # check skipped because the B-rep was unavailable. Distinguishing "did not
    # run" from "passed" matters: silently treating the former as the latter is
    # how a broken validator ships broken models.
    skipped: list[str] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    # -- queries -----------------------------------------------------------
    @property
    def hard_failures(self) -> list[Check]:
        return [c for c in self.checks if c.is_hard_failure]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.is_warning]

    @property
    def passed(self) -> bool:
        """Delivery gate: no hard failures. Warnings do not block."""
        return not self.hard_failures

    def by_tier(self, tier: int) -> list[Check]:
        return [c for c in self.checks if c.tier == tier]

    def get(self, check_id: str) -> Check | None:
        for check in self.checks:
            if check.id == check_id:
                return check
        return None

    # -- serialisation -----------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "profile": self.profile_id,
            "material": self.material,
            "summary": {
                "checks": len(self.checks),
                "failures": len(self.hard_failures),
                "warnings": len(self.warnings),
                "skipped": len(self.skipped),
            },
            "hard_failures": [c.as_dict() for c in self.hard_failures],
            "warnings": [c.as_dict() for c in self.warnings],
            "checks": [c.as_dict() for c in self.checks],
            "measurements": self.measurements,
            "skipped": self.skipped,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, default=_json_default)

    # -- rendering ---------------------------------------------------------
    def agent_feedback(self) -> str:
        """The report as a repair prompt.

        Deliberately terse and numeric. A model given prose has to infer the
        numbers; a model given the numbers can change them.
        """
        if self.passed and not self.warnings:
            return "VALIDATION PASSED: no failures, no warnings."

        lines: list[str] = []
        if self.hard_failures:
            lines.append("VALIDATION FAILED. These must be fixed:")
            for check in self.hard_failures:
                lines.append(f"  [{check.id}] {check.describe()}")
        else:
            lines.append("VALIDATION PASSED with warnings.")

        if self.warnings:
            lines.append("")
            lines.append("Warnings (fix if you are regenerating anyway):")
            for check in self.warnings:
                lines.append(f"  [{check.id}] {check.describe()}")

        if self.measurements:
            lines.append("")
            lines.append("Measurements:")
            for key in sorted(self.measurements):
                value = self.measurements[key]
                if isinstance(value, (int, float, str, bool)):
                    lines.append(f"  {key}: {_fmt(value)}")
        return "\n".join(lines)

    def summary_line(self) -> str:
        if self.passed and not self.warnings:
            return f"{len(self.checks)} checks passed"
        bits = []
        if self.hard_failures:
            bits.append(f"{len(self.hard_failures)} failure(s)")
        if self.warnings:
            bits.append(f"{len(self.warnings)} warning(s)")
        return ", ".join(bits) + f" across {len(self.checks)} checks"


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3g}"
    if isinstance(value, (list, tuple)):
        return " x ".join(_fmt(v) for v in value)
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


# ---------------------------------------------------------------------------
# Construction helpers used by the tier modules.
# ---------------------------------------------------------------------------


def check(
    check_id: str,
    tier: int,
    title: str,
    ok: bool,
    *,
    severity: Severity = Severity.FAIL,
    message: str = "",
    measured: Any = None,
    threshold: Any = None,
    unit: str | None = None,
    location_mm: list[float] | None = None,
    remedy: str | None = None,
) -> Check:
    return Check(
        id=check_id,
        tier=tier,
        title=title,
        severity=severity if not ok else Severity.PASS,
        passed=ok,
        message=message or (f"{title}: ok" if ok else f"{title}: failed"),
        measured=measured,
        threshold=threshold,
        unit=unit,
        location_mm=location_mm,
        remedy=remedy if not ok else None,
    )
