"""Step 1 of the agent loop: turn a sentence into a structured request.

Two implementations of the same output, and the second is not a toy:

* `parse_with_model` asks Claude for a structured intent. Better at category
  inference, unit conversion in prose ("about four inches across"), and
  spotting which functional dimension is missing.
* `parse_heuristically` extracts what a regex reliably can -- explicit
  dimensions, quoted text, category keywords, mount type, material. This is what
  runs with no API key, and it is genuinely sufficient for the template path,
  which is where most traffic should go anyway.

The clarification rule is from spec section 5.1 and matters more than it looks:
**ask only about missing functional parameters, never about style.** A user who
says "a wall planter" and is asked "what size?" feels helped. The same user
asked "what aesthetic are you going for?" feels interrogated, and the question
buys nothing -- the template has a default and the model can pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..dfm import CATEGORIES, DEFAULT_PROFILE_ID
from ..llm import Tier, extract_json

# Which dimensions a category cannot be built without. Everything else has a
# defensible default; these do not, because getting them wrong wastes filament.
REQUIRED_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "planter": ("width_mm",),
    "organizer": ("length_mm", "width_mm"),
    "box": ("length_mm", "width_mm"),
    "wall_decor": (),
    "keychain": (),
    "hook": (),
}

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "keychain": (
        "keychain", "key chain", "keyring", "key ring", "key fob", "fob",
        "luggage tag", "bag tag", "name tag", "dog tag", "bottle opener", "charm",
    ),
    "planter": (
        "planter", "plant pot", "flower pot", "pot for", "succulent", "cactus",
        "herb pot", "vase", "propagation", "plant holder", "planter box",
    ),
    "organizer": (
        "organizer", "organiser", "pen cup", "pencil holder", "desk tidy",
        "drawer divider", "tray", "caddy", "cable clip", "cable management",
        "cable tidy", "headphone stand", "coin tray", "utensil", "sorter",
    ),
    "wall_decor": (
        "wall art", "wall decor", "wall décor", "sign", "plaque", "monogram",
        "silhouette", "wall panel", "wall tile", "hex tile", "letter", "nameplate",
    ),
    "hook": (
        "hook", "coat hook", "key hook", "towel hook", "peg", "hanger",
        "french cleat", "wall mount", "bracket",
    ),
    "nature": (
        "mushroom", "toadstool", "fungus", "fungi", "amanita", "figurine",
        "ornament", "sculpture", "statuette", "miniature", "leaf", "shell",
    ),
    "box": (
        "box", "case", "container", "lid", "bin", "storage", "gridfinity",
        "stash", "enclosure",
    ),
}

_MOUNT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "keyhole_slot": ("keyhole", "key hole", "hang on a screw", "hangs on a screw"),
    "flush_screw": ("screw", "screwed", "screw mount", "wall screw", "anchor"),
    "french_cleat": ("french cleat", "cleat"),
    "adhesive_pad": ("adhesive", "sticky", "command strip", "double sided tape", "3m"),
}

_MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU")

# Nursery pot sizes, which people give in inches and mean as a pot diameter.
_INCH = 25.4


@dataclass
class ParsedIntent:
    """The structured form of a request (spec section 5.1)."""

    prompt: str
    category: str | None = None
    subject: str = ""
    dimensions: dict[str, float] = field(default_factory=dict)
    text_content: str | None = None
    mount_type: str | None = None
    style: str | None = None
    printer_profile: str = DEFAULT_PROFILE_ID
    material: str = "PLA"
    quantity: int = 1
    constraints: dict[str, Any] = field(default_factory=dict)
    # Set when a functional dimension is missing and the caller allowed
    # clarification.
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    source: str = "heuristic"
    notes: str = ""

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications)

    def search_query(self) -> str:
        """The text the template matcher searches with.

        Built from the parsed fields rather than the raw prompt so that filler
        ("can you make me...", "I'd like...") does not dilute the match, while
        keeping the subject and category words that carry the signal.
        """
        parts = [self.subject or self.prompt]
        if self.category:
            parts.append(self.category.replace("_", " "))
        if self.mount_type:
            parts.append(self.mount_type.replace("_", " "))
        if self.style:
            parts.append(self.style)
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "subject": self.subject,
            "dimensions": self.dimensions,
            "text_content": self.text_content,
            "mount_type": self.mount_type,
            "style": self.style,
            "printer_profile": self.printer_profile,
            "material": self.material,
            "quantity": self.quantity,
            "constraints": self.constraints,
            "source": self.source,
            "notes": self.notes,
        }

    def summary(self) -> str:
        bits = [f"category={self.category or 'unknown'}"]
        if self.dimensions:
            bits.append(
                "dimensions=" + ", ".join(f"{k}={v:g}" for k, v in sorted(self.dimensions.items()))
            )
        if self.text_content:
            bits.append(f'text="{self.text_content}"')
        if self.mount_type:
            bits.append(f"mount={self.mount_type}")
        return " ".join(bits)


# ---------------------------------------------------------------------------
# Heuristic parsing
# ---------------------------------------------------------------------------

_DIMENSION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|"
    r"m|met(?:er|re)s?|ft|feet|foot|in|inch(?:es)?|\"|')\b",
    re.IGNORECASE,
)

_TRIPLE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?))?\s*"
    r"(mm|cm|in|inch(?:es)?|\")?",
    re.IGNORECASE,
)

# Words that attach a number to a specific axis.
_AXIS_WORDS: dict[str, tuple[str, ...]] = {
    "width_mm": ("wide", "width", "across", "diameter", "wide by"),
    "length_mm": ("long", "length", "deep long"),
    "height_mm": ("tall", "high", "height", "deep" ),
    "depth_mm": ("depth", "front to back"),
}

_QUOTED_RE = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{1,40})[\"“”'‘’]")
_SAYS_RE = re.compile(
    r"(?:that\s+)?(?:says?|reading|labell?ed|with\s+the\s+(?:word|name|text))\s+"
    r"[\"“'‘]?([A-Za-z0-9 &'.\-]{1,30}?)[\"”'’]?(?:\s*(?:on|$|[,.]))",
    re.IGNORECASE,
)


def to_mm(value: float, unit: str) -> float:
    """Normalise a dimension to millimetres.

    Metres and feet are handled not because anyone prints at that scale, but
    because someone occasionally asks for something that size and the request
    has to be measurable before it can be rejected. Silently reading "3 metres"
    as 3 mm would build a valid, useless model instead.
    """
    unit = unit.lower().strip()
    if unit.startswith("cm") or unit.startswith("centim"):
        return value * 10.0
    if unit in {'"', "in"} or unit.startswith("inch"):
        return value * _INCH
    if unit in {"'", "ft", "feet", "foot"}:
        return value * _INCH * 12
    if unit == "m" or unit.startswith("met"):
        return value * 1000.0
    return value


def parse_heuristically(
    prompt: str,
    *,
    printer_profile: str = DEFAULT_PROFILE_ID,
    material: str = "PLA",
    interactive: bool = True,
) -> ParsedIntent:
    """Extract intent with regexes only. No model call."""
    lowered = prompt.lower()
    intent = ParsedIntent(
        prompt=prompt,
        printer_profile=printer_profile,
        material=material,
        source="heuristic",
    )

    intent.category = _detect_category(lowered)
    intent.subject = _detect_subject(prompt)
    intent.dimensions = _detect_dimensions(prompt)
    intent.text_content = _detect_text(prompt)
    intent.mount_type = _detect_mount(lowered)
    intent.material = _detect_material(prompt) or material
    intent.constraints = _detect_constraints(lowered)

    if interactive:
        intent.clarifications = missing_functional_dimensions(intent)
    return intent


def _detect_category(lowered: str) -> str | None:
    """Longest keyword wins, so "planter box" is a planter rather than a box."""
    best: tuple[int, str] | None = None
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered and (best is None or len(keyword) > best[0]):
                best = (len(keyword), category)
    return best[1] if best else None


def _detect_subject(prompt: str) -> str:
    """Strip the request framing, keep the noun phrase."""
    cleaned = re.sub(
        r"^\s*(?:can you\s+)?(?:please\s+)?(?:make|create|design|generate|build|"
        r"i\s+(?:want|need|would like)|i'?d like)\s+(?:me\s+)?(?:a|an|some|the)?\s*",
        "",
        prompt.strip(),
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .!?")


def _detect_dimensions(prompt: str) -> dict[str, float]:
    """Pull dimensions out, preferring ones an axis word identifies.

    A bare "60 x 40 mm" is assigned to length and width in order; a value with
    an axis word next to it ("90 mm tall") overrides that, because an explicit
    statement should always beat positional inference.
    """
    dimensions: dict[str, float] = {}

    triple = _TRIPLE_RE.search(prompt)
    if triple:
        unit = triple.group(4) or "mm"
        values = [float(g) for g in triple.groups()[:3] if g]
        axes = ["length_mm", "width_mm", "height_mm"]
        for axis, value in zip(axes, values):
            dimensions[axis] = round(to_mm(value, unit), 3)

    for match in _DIMENSION_RE.finditer(prompt):
        value = to_mm(float(match.group("value")), match.group("unit"))
        window = prompt[match.end() : match.end() + 24].lower()
        before = prompt[max(0, match.start() - 24) : match.start()].lower()
        axis = _axis_for(window, before)
        if axis:
            dimensions[axis] = round(value, 3)
        elif not dimensions:
            dimensions["width_mm"] = round(value, 3)

    # "for a 4 inch pot" is a pot diameter, which is the planter's inner width.
    pot = re.search(
        r"(?:for\s+an?\s+)?(\d+(?:\.\d+)?)\s*(?:in|inch(?:es)?|\")\s*(?:nursery\s*)?pot",
        prompt,
        re.IGNORECASE,
    )
    if pot:
        dimensions["width_mm"] = round(float(pot.group(1)) * _INCH, 1)
    return dimensions


def _axis_for(after: str, before: str) -> str | None:
    for axis, words in _AXIS_WORDS.items():
        for word in words:
            if after.strip().startswith(word) or before.rstrip().endswith(word):
                return axis
    return None


def _detect_text(prompt: str) -> str | None:
    """Text to put on the model: quoted, or introduced by 'that says'."""
    says = _SAYS_RE.search(prompt)
    if says:
        candidate = says.group(1).strip()
        if candidate:
            return candidate
    quoted = _QUOTED_RE.search(prompt)
    if quoted:
        return quoted.group(1).strip()
    return None


def _detect_mount(lowered: str) -> str | None:
    for mount, keywords in _MOUNT_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return mount
    return None


def _detect_material(prompt: str) -> str | None:
    for material in _MATERIALS:
        if re.search(rf"\b{material}\b", prompt, re.IGNORECASE):
            return material
    return None


def _detect_constraints(lowered: str) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    if any(
        phrase in lowered
        for phrase in ("no support", "without support", "supportless", "support free")
    ):
        constraints["avoid_supports"] = True
    if "watertight" in lowered or "hold water" in lowered:
        constraints["watertight"] = True
    if "stackable" in lowered or "stacks" in lowered:
        constraints["stackable"] = True
    return constraints


def missing_functional_dimensions(intent: ParsedIntent) -> list[dict[str, Any]]:
    """Which functional dimensions are missing, as answerable questions.

    Never asks about style. A question here costs the user a round trip, so it
    has to be one where guessing wrong wastes a print.
    """
    required = REQUIRED_DIMENSIONS.get(intent.category or "", ())
    questions: list[dict[str, Any]] = []
    for key in required:
        if key in intent.dimensions:
            continue
        questions.append(
            {
                "parameter": key,
                "question": _question_for(intent.category, key),
                "unit": "mm",
            }
        )
    return questions


def _question_for(category: str | None, key: str) -> str:
    axis = key.replace("_mm", "")
    if category == "planter":
        return (
            "How wide should the planter be? If you have a pot to fit, give me "
            "its diameter and I will size around it."
        )
    if category in {"organizer", "box"}:
        return (
            f"What {axis} should it be? Measuring the drawer or shelf it has to "
            "fit is usually the number you want."
        )
    return f"What {axis} should it be, in millimetres?"


# ---------------------------------------------------------------------------
# Model parsing
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = f"""\
You turn a natural-language request for a 3D-printed object into a structured \
intent. You do not design anything and you do not write code.

Return a JSON object only, with these fields:

  category         one of {list(CATEGORIES)}, or null if none fits
  subject          a short noun phrase for what the object is
  dimensions       object with any of length_mm, width_mm, height_mm, depth_mm, \
diameter_mm -- always in millimetres, converting from inches or centimetres \
yourself
  text_content     any text to appear on the model, or null
  mount_type       one of keyhole_slot, flush_screw, french_cleat, adhesive_pad, \
or null
  style            a short descriptive word if the user gave one, else null
  material         PLA, PETG, ABS, ASA or TPU -- PLA unless the user said otherwise
  quantity         integer, 1 unless stated
  constraints      object; may include avoid_supports, watertight, stackable, \
max_print_time_min
  missing          array of functional parameter names the user did not give and \
that cannot be sensibly defaulted

Rules:

- Convert every dimension to millimetres. "4 inch pot" is 101.6 mm.
- Put a dimension on the axis the user meant. "90 mm tall" is height_mm, not \
width_mm.
- `missing` is for *functional* parameters only -- a size that changes whether \
the object works. Never list a style, colour, finish or aesthetic preference. \
Asking a user what aesthetic they want is worse than picking one.
- If the user gave enough to build something reasonable, `missing` is empty. \
Prefer a sensible default over a question."""


def parse_with_model(
    prompt: str,
    client,
    *,
    printer_profile: str = DEFAULT_PROFILE_ID,
    material: str = "PLA",
    interactive: bool = True,
) -> ParsedIntent:
    """Parse intent with Claude, falling back to the heuristic path on failure.

    The fallback is not a formality. Intent parsing is the first step of every
    generation, so a transient API failure here would otherwise fail requests
    the template path could have served perfectly well.
    """
    fallback = parse_heuristically(
        prompt,
        printer_profile=printer_profile,
        material=material,
        interactive=interactive,
    )
    if not getattr(client, "available", False):
        return fallback

    try:
        completion = client.complete(
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            tier=Tier.FAST,
            max_tokens=1200,
            effort="low",
            purpose="intent parsing",
        )
    except Exception:
        fallback.notes = "intent parsed heuristically: the model call failed"
        return fallback

    payload = completion.json_block() or extract_json(completion.text)
    if not payload:
        fallback.notes = "intent parsed heuristically: the model returned no JSON"
        return fallback

    return _intent_from_payload(
        payload,
        prompt,
        printer_profile=printer_profile,
        material=material,
        interactive=interactive,
        fallback=fallback,
    )


def _intent_from_payload(
    payload: dict,
    prompt: str,
    *,
    printer_profile: str,
    material: str,
    interactive: bool,
    fallback: ParsedIntent,
) -> ParsedIntent:
    category = payload.get("category")
    if category not in CATEGORIES:
        category = fallback.category

    dimensions: dict[str, float] = {}
    raw_dimensions = payload.get("dimensions")
    if isinstance(raw_dimensions, dict):
        for key, value in raw_dimensions.items():
            if isinstance(value, (int, float)) and value > 0:
                dimensions[str(key)] = float(value)
    # Keep anything the regex found that the model omitted: an explicit number
    # in the prompt is the strongest signal available and should never be lost.
    for key, value in fallback.dimensions.items():
        dimensions.setdefault(key, value)

    intent = ParsedIntent(
        prompt=prompt,
        category=category,
        subject=str(payload.get("subject") or fallback.subject),
        dimensions=dimensions,
        text_content=payload.get("text_content") or fallback.text_content,
        mount_type=payload.get("mount_type") or fallback.mount_type,
        style=payload.get("style"),
        printer_profile=printer_profile,
        material=str(payload.get("material") or material).upper(),
        quantity=int(payload.get("quantity") or 1),
        constraints={**fallback.constraints, **(payload.get("constraints") or {})},
        source="model",
    )

    if interactive:
        named = payload.get("missing")
        if isinstance(named, list) and named:
            intent.clarifications = [
                {
                    "parameter": str(item),
                    "question": _question_for(category, str(item)),
                    "unit": "mm",
                }
                for item in named
                if isinstance(item, (str, int))
            ]
        else:
            intent.clarifications = missing_functional_dimensions(intent)
    return intent


def parse(
    prompt: str,
    client=None,
    *,
    printer_profile: str = DEFAULT_PROFILE_ID,
    material: str = "PLA",
    interactive: bool = True,
) -> ParsedIntent:
    """Parse a request, using a model when one is available."""
    if client is not None and getattr(client, "available", False):
        return parse_with_model(
            prompt,
            client,
            printer_profile=printer_profile,
            material=material,
            interactive=interactive,
        )
    return parse_heuristically(
        prompt,
        printer_profile=printer_profile,
        material=material,
        interactive=interactive,
    )
