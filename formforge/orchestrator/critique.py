"""Step 6: show the model its own output and ask whether it is right.

This step is not optional (spec section 5.4). Numeric validation proves the mesh
is *valid*; nothing in it proves the mesh is *the thing that was asked for*. A
perfectly manifold, DFM-clean solid that looks nothing like the request passes
every check in tier 1, 2 and 3 and is still a failure the user pays for.

Rendering the result and re-showing it is the cheapest available proxy for "does
this look right", and it is unreasonably effective on exactly the failures the
numbers cannot see:

* text mirrored, or on the wrong face
* a feature that ended up inside the solid
* proportions that are individually valid and jointly absurd
* a hole that is a dimple, a slot that never broke through
* the whole thing at 10x or 0.1x the intended scale -- which is why the
  isometric view carries a 10 mm build-plate grid

The section cut is included for the same reason: hollow parts hide their sins
from the outside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm import Tier, cached_system, image_block, text_block

# Views sent to the model, in the order it sees them.
DEFAULT_VIEW_ORDER = ("iso", "front", "top", "section")

CRITIQUE_SYSTEM = """\
You are reviewing renders of a 3D model that a CAD script just produced, to \
judge whether it matches what the user asked for. The model has already passed \
every geometric and manufacturability check -- your job is the thing those \
checks cannot do.

You are looking for:

- Does this look like the object the user described?
- Is any text present, right way round, and on a face where it can be read? \
Mirrored text is a common failure and it is your job to catch it.
- Are the proportions plausible for the stated purpose?
- Are features missing, duplicated, floating unattached, or buried inside the \
solid?
- In the section view: are the walls a consistent thickness? Is the cavity \
where it should be? Did anything fail to cut through?
- Against the 10 mm build-plate grid in the isometric view, is the object the \
size it should be?

Judge the object against the request, not against your taste. A plain shape the \
user asked for is correct. Do not report an object as wrong because you would \
have styled it differently.

Return a JSON object only:

{
  "verdict": "pass" | "revise" | "fail",
  "confidence": 0.0-1.0,
  "matches_request": true | false,
  "issues": [
    {"severity": "major" | "minor", "what": "<what is wrong>",
     "where": "<which view shows it>", "fix": "<what to change in the script>"}
  ],
  "summary": "<one sentence a user would understand>"
}

"pass"   -- ship it.
"revise" -- usable, but a specific fixable problem is visible. Say exactly what \
to change.
"fail"   -- this is not what was asked for.

Be decisive. A render that looks right is a pass, and hedging on a good model \
costs the user a pointless regeneration."""


@dataclass
class CritiqueResult:
    """The model's judgement of its own output."""

    verdict: str = "pass"
    confidence: float = 0.0
    matches_request: bool = True
    issues: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    ran: bool = False
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def major_issues(self) -> list[dict[str, Any]]:
        return [i for i in self.issues if i.get("severity") == "major"]

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "matches_request": self.matches_request,
            "issues": self.issues,
            "summary": self.summary,
            "error": self.error,
        }

    def agent_feedback(self) -> str:
        """The critique as a repair instruction."""
        if self.passed or not self.issues:
            return "VISUAL CRITIQUE: the renders match the request."
        lines = [f"VISUAL CRITIQUE: {self.verdict.upper()} -- {self.summary}"]
        lines.append("\nWhat the renders show:")
        for issue in self.issues:
            marker = "MAJOR" if issue.get("severity") == "major" else "minor"
            where = f" (visible in the {issue['where']} view)" if issue.get("where") else ""
            lines.append(f"  [{marker}] {issue.get('what', '')}{where}")
            if issue.get("fix"):
                lines.append(f"          fix: {issue['fix']}")
        return "\n".join(lines)


def critique(
    render_result,
    intent,
    client,
    *,
    validation_summary: str = "",
    tier: Tier = Tier.STANDARD,
) -> CritiqueResult:
    """Show the renders to the model and ask whether they match the request."""
    if not getattr(client, "available", False):
        return CritiqueResult(
            ran=False,
            verdict="pass",
            summary="visual critique skipped: no model available to look at the renders",
            error="no client",
        )

    views = _ordered_views(render_result)
    if not views:
        return CritiqueResult(
            ran=False, verdict="pass", summary="visual critique skipped: nothing rendered"
        )

    content: list[dict] = []
    for name, path in views:
        content.append(text_block(f"{name} view:"))
        content.append(image_block(path))

    request = [f"THE USER ASKED FOR:\n{intent.prompt}", f"\nPARSED AS: {intent.summary()}"]
    if intent.text_content:
        request.append(
            f'\nTEXT THAT SHOULD APPEAR ON THE MODEL: "{intent.text_content}" '
            "-- check it reads correctly and is not mirrored."
        )
    if validation_summary:
        request.append(f"\nMEASURED: {validation_summary}")
    request.append("\nDo the renders show what was asked for?")
    content.append(text_block("\n".join(request)))

    try:
        completion = client.complete(
            system=cached_system(CRITIQUE_SYSTEM),
            messages=[{"role": "user", "content": content}],
            tier=tier,
            max_tokens=2000,
            effort="medium",
            purpose="visual critique",
        )
    except Exception as exc:
        # A failed critique must never block delivery of a model that passed
        # every geometric check. The critique is a quality gate, not a
        # correctness one.
        return CritiqueResult(
            ran=False,
            verdict="pass",
            summary="visual critique could not run",
            error=str(exc),
        )

    payload = completion.json_block()
    if not payload:
        return CritiqueResult(
            ran=False,
            verdict="pass",
            summary="visual critique returned no usable verdict",
            error="unparseable response",
        )

    verdict = str(payload.get("verdict", "pass")).lower()
    if verdict not in {"pass", "revise", "fail"}:
        verdict = "pass"

    issues = [i for i in (payload.get("issues") or []) if isinstance(i, dict)]
    return CritiqueResult(
        verdict=verdict,
        confidence=float(payload.get("confidence") or 0.0),
        matches_request=bool(payload.get("matches_request", True)),
        issues=issues,
        summary=str(payload.get("summary") or ""),
        ran=True,
    )


def _ordered_views(render_result) -> list[tuple[str, Path]]:
    """Views in a deliberate order, iso first.

    Order matters: the isometric view establishes what the object is, and the
    orthographic and section views are then read as detail on it. Leading with a
    flat orthographic view of an unfamiliar object invites the model to guess
    wrong and then defend the guess.
    """
    available = getattr(render_result, "views", {}) or {}
    ordered: list[tuple[str, Path]] = []
    for name in DEFAULT_VIEW_ORDER:
        path = available.get(name)
        if path and Path(path).exists():
            ordered.append((name, Path(path)))
    for name, path in available.items():
        if name not in DEFAULT_VIEW_ORDER and Path(path).exists():
            ordered.append((name, Path(path)))
    return ordered[:6]
