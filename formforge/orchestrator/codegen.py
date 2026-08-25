"""Steps 3: parameter fill and freeform code generation (spec sections 5, 6.3).

Two paths with very different economics:

* **Template fill.** The model chooses values for a JSON Schema. Deterministic,
  one cheap call, near-100% success -- there is no geometry to get wrong because
  the geometry is already written and print-tested.
* **Freeform codegen.** The model writes build123d from scratch, given the DFM
  rules, the API cheat-sheet, retrieved exemplar templates, and any prior
  failure from this session. Five to ten times the cost and far more failure
  modes.

Both share one prompt structure, and the ordering of its parts is the whole
caching strategy: stable blocks first (rules, cheat-sheet, registry summary),
request-specific content last. Anything that varies inside the prefix -- a
timestamp, an unsorted dict, a per-request id -- silently costs a cache miss on
every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..dfm import category_rules_block, rules_block
from ..llm import Tier, cached_system, extract_code, extract_json
from .cheatsheet import cheatsheet, registry_summary


@dataclass
class CodegenResult:
    """A generated script (or filled parameters), plus how it was produced."""

    source: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    exposed_params: dict[str, Any] = field(default_factory=dict)
    language: str = "build123d"
    notes: str = ""
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "language": self.language,
            "params": self.params,
            "exposed_params": self.exposed_params,
            "notes": self.notes,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Template parameter fill
# ---------------------------------------------------------------------------

PARAM_FILL_SYSTEM = """\
You fill in the parameters of a print-tested 3D model template. You do not write \
code and you do not design anything -- the geometry already exists and works.

You are given a JSON Schema and a user's request. Return a JSON object of \
parameter values only.

Rules:

- Respect every minimum and maximum. A value outside the schema is rejected, \
which costs the user a round trip for nothing.
- Use the user's stated dimensions exactly. Do not round a stated 120 mm to 125 \
because it is a nicer number.
- Where the user said nothing, keep the default. The defaults are the tested \
configuration; changing one you were not asked about is how a validated \
template starts producing parts that fail.
- Read each parameter's description. They record what actually broke in \
testing, not general advice.
- Return only the parameters you are setting. Anything you omit keeps its \
default."""


def fill_template_params(
    template,
    intent,
    client,
    *,
    tier: Tier = Tier.FAST,
) -> CodegenResult:
    """Choose template parameter values for a request.

    Falls back to mapping the parsed dimensions onto the schema directly when no
    model is available, which is what makes the template path work offline.
    """
    heuristic = _heuristic_params(template, intent)
    if not getattr(client, "available", False):
        return CodegenResult(
            source=template.render_source(heuristic),
            params=template.merge_params(heuristic),
            language=template.language,
            notes="parameters filled from the parsed dimensions (no model available)",
        )

    request = json.dumps(
        {
            "user_request": intent.prompt,
            "parsed": intent.as_dict(),
            "template": {
                "id": template.id,
                "display_name": template.display_name,
                "description": template.description,
                "param_schema": template.param_schema,
            },
        },
        indent=2,
        sort_keys=True,
    )

    try:
        completion = client.complete(
            system=cached_system(PARAM_FILL_SYSTEM),
            messages=[{"role": "user", "content": request}],
            tier=tier,
            max_tokens=1500,
            effort="low",
            purpose="template parameter fill",
        )
    except Exception as exc:
        return CodegenResult(
            source=template.render_source(heuristic),
            params=template.merge_params(heuristic),
            language=template.language,
            notes=f"parameters filled heuristically: {exc}",
        )

    payload = completion.json_block() or {}
    chosen = {k: v for k, v in payload.items() if k in template.properties}
    # The parsed dimensions win over anything the model omitted, never over what
    # it set: an explicit number in the prompt must survive to the geometry.
    for key, value in heuristic.items():
        chosen.setdefault(key, value)

    problems = template.validate_params(template.merge_params(chosen))
    if problems:
        # Rather than fail, drop the offending values back to their defaults and
        # record it. A template with 12 parameters should not fail because the
        # model picked one out-of-range value for a knob nobody asked about.
        chosen = _drop_invalid(template, chosen)
        note = "some parameter values were out of range and were reset: " + "; ".join(
            problems[:4]
        )
    else:
        note = ""

    merged = template.merge_params(chosen)
    return CodegenResult(
        source=template.render_source(merged),
        params=merged,
        language=template.language,
        notes=note,
    )


def _heuristic_params(template, intent) -> dict[str, Any]:
    """Map parsed intent onto a template's schema without a model.

    Matches by exact parameter name first, then by the axis word inside it, so
    `width_mm` from the intent reaches `across_flats_mm` on the hex planter --
    which is what a user means by "how wide".
    """
    chosen: dict[str, Any] = {}
    properties = template.properties

    for key, value in intent.dimensions.items():
        if key in properties:
            chosen[key] = value
            continue
        axis = key.replace("_mm", "")
        for name in properties:
            if name in chosen:
                continue
            if axis in name and name.endswith("_mm"):
                chosen[name] = value
                break

    # A width with nowhere to go still has an obvious home on a round or
    # regular-polygon part.
    if "width_mm" in intent.dimensions:
        for name in ("across_flats_mm", "plate_d_mm", "outer_l_mm", "body_l_mm"):
            if name in properties and name not in chosen:
                chosen[name] = intent.dimensions["width_mm"]
                break

    if intent.text_content:
        for name in ("text", "label", "caption"):
            if name in properties:
                chosen[name] = intent.text_content
                break

    if intent.mount_type and "mount" in properties:
        allowed = properties["mount"].get("enum") or []
        if intent.mount_type in allowed:
            chosen["mount"] = intent.mount_type

    return {
        key: value
        for key, value in chosen.items()
        if not _out_of_range(properties.get(key), value)
    }


def _out_of_range(spec: dict | None, value: Any) -> bool:
    if not isinstance(spec, dict) or not isinstance(value, (int, float)):
        return False
    minimum, maximum = spec.get("minimum"), spec.get("maximum")
    if minimum is not None and value < minimum:
        return True
    return maximum is not None and value > maximum


def _drop_invalid(template, chosen: dict[str, Any]) -> dict[str, Any]:
    """Remove only the values that fail validation, keeping the rest."""
    kept: dict[str, Any] = {}
    for key, value in chosen.items():
        candidate = dict(kept)
        candidate[key] = value
        if not template.validate_params(template.merge_params(candidate)):
            kept = candidate
    return kept


# ---------------------------------------------------------------------------
# Freeform codegen
# ---------------------------------------------------------------------------

FREEFORM_SYSTEM_TASK = """\
You write build123d Python scripts that produce functional, dimensioned, \
printable solids. You are the geometry author for a system that hands the \
result straight to a slicer, so "roughly right" is a failure.

Output format: a single fenced Python code block, then a single fenced JSON \
block declaring the parameters you exposed. No prose before, between or after.

```python
from build123d import *

WIDTH_MM = 60.0
...
result = part.part
```

```json
{"WIDTH_MM": {"min": 40, "max": 120, "step": 5, "label": "Width"}}
```

Hard requirements:

1. Every dimension is a named UPPER_CASE constant at module top. A numeric \
literal inside a geometry call is rejected before the script is even run -- \
this is what makes the result editable and drives the parameter sliders.
2. Assign the finished solid to a module-level variable named `result`.
3. Produce exactly one solid unless the request explicitly asks for separate \
parts.
4. Import only build123d, math and numpy.
5. Follow the DFM rules you were given. They are printer limits, not style \
preferences.

Derive dependent values from the constants rather than restating them: if the \
wall is WALL_MM, the cavity is OUTER_MM - 2*WALL_MM, not a second constant that \
can drift out of agreement with the first."""


def build_freeform_system(
    *,
    printer_profile: str,
    material: str,
    category: str | None,
    templates: list | None = None,
) -> list[dict]:
    """Assemble the cached system prompt for freeform generation.

    Block order is the caching strategy. The first three blocks are identical
    for every request with the same printer and material, so they cache; the
    task instructions come last. Roughly 8-12k tokens of prefix that would
    otherwise dominate the input cost of every call (spec section 5.2).
    """
    return cached_system(
        rules_block(printer_profile, material) + category_rules_block(category),
        cheatsheet(),
        registry_summary(templates or []),
        FREEFORM_SYSTEM_TASK,
    )


def build_freeform_request(
    intent,
    *,
    exemplars: list | None = None,
    failures: list[str] | None = None,
) -> str:
    """The per-request half of the codegen prompt."""
    sections: list[str] = [f"REQUEST\n{intent.prompt}", f"\nPARSED INTENT\n{intent.summary()}"]

    if intent.dimensions:
        sections.append(
            "\nDIMENSIONS THE USER GAVE (the bounding box is checked against these)\n"
            + "\n".join(f"  {k} = {v:g} mm" for k, v in sorted(intent.dimensions.items()))
        )
    if intent.text_content:
        sections.append(f'\nTEXT ON THE MODEL\n  "{intent.text_content}"')
    if intent.constraints:
        sections.append(
            "\nCONSTRAINTS\n"
            + "\n".join(f"  {k}: {v}" for k, v in sorted(intent.constraints.items()))
        )

    for template in exemplars or []:
        sections.append(
            f"\nEXEMPLAR -- {template.display_name} ({template.id})\n"
            f"A working, print-tested script for a related part. Follow its "
            f"structure and its selection style.\n\n```python\n{template.source}\n```"
        )

    if failures:
        sections.append(
            "\nPREVIOUS ATTEMPTS IN THIS SESSION FAILED. Fix the cause; do not "
            "restate the same script.\n" + "\n\n".join(failures[-2:])
        )

    return "\n".join(sections)


def generate_freeform(
    intent,
    client,
    *,
    exemplars: list | None = None,
    failures: list[str] | None = None,
    templates: list | None = None,
    tier: Tier = Tier.STANDARD,
    max_tokens: int = 8000,
) -> CodegenResult:
    """Write a build123d script from scratch."""
    if not getattr(client, "available", False):
        return CodegenResult(
            ok=False,
            error="Freeform generation needs a Claude API client, and none is "
            "configured. Nothing in the template registry matched this request "
            "closely enough to use instead.",
        )

    system = build_freeform_system(
        printer_profile=intent.printer_profile,
        material=intent.material,
        category=intent.category,
        templates=templates,
    )
    request = build_freeform_request(intent, exemplars=exemplars, failures=failures)

    try:
        completion = client.complete(
            system=system,
            messages=[{"role": "user", "content": request}],
            tier=tier,
            max_tokens=max_tokens,
            effort="high",
            purpose="freeform code generation",
        )
    except Exception as exc:
        return CodegenResult(ok=False, error=str(exc))

    if completion.refused:
        return CodegenResult(
            ok=False,
            error=f"the model declined this request ({completion.refusal_category or 'policy'})",
        )

    source = extract_code(completion.text)
    if not source or "result" not in source:
        return CodegenResult(
            ok=False,
            error="the model did not return a usable script (no `result` assignment)",
        )

    exposed = _exposed_params(completion.text)
    return CodegenResult(
        source=source,
        exposed_params=exposed,
        language="build123d",
        notes=f"freeform generation on {completion.model}",
    )


def _exposed_params(text: str) -> dict[str, Any]:
    """Read the parameter-slider declaration out of the response.

    Returns empty rather than raising when it is missing: the script is still
    usable, the UI just has no sliders for it, and failing a valid generation
    over a missing metadata block would be the wrong trade.
    """
    blocks = [
        block
        for block in _json_blocks(text)
        if isinstance(block, dict) and block and all(isinstance(v, dict) for v in block.values())
    ]
    return blocks[-1] if blocks else {}


def _json_blocks(text: str) -> list[Any]:
    import re  # noqa: PLC0415

    found: list[Any] = []
    for match in re.finditer(r"```json\s*\n(.*?)```", text, re.DOTALL):
        try:
            found.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    if not found:
        payload = extract_json(text)
        if payload:
            found.append(payload)
    return found
