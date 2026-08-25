"""The template registry (spec section 6.2).

Every template is a hand-authored, human-reviewed parametric definition that
builds and validates across its whole declared parameter range. Each carries an
explicit `print_test` status; none of the templates in this repository has been
physically printed yet, and nothing in the system may claim otherwise.

The registry is the single highest-leverage component in the system for two
independent reasons:

* **Reliability.** A template hit is a schema fill, not a code-generation
  problem. There is no chance the model writes a broken boolean, because it does
  not write geometry at all.
* **Cost.** The freeform path is five to ten times the price of the template
  path (spec section 16), so every template added moves traffic off the
  expensive route. Template coverage is the primary cost lever in the product.

Matching is deliberately pluggable. The production system embeds
`embedding_text` and does a cosine search; the default here is a lexical scorer
that needs no model and no network, so the registry is testable and the system
runs end to end without an API key. The routing thresholds are calibrated per
backend, because a cosine score and a lexical score are not the same number.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import binding, security

TEMPLATE_DIR = Path(__file__).parent / "templates"


class Route(str, Enum):
    """Which generation path a match implies (spec section 6.2)."""

    TEMPLATE = "template"
    # Close but not exact: use the template's source as a starting point for
    # freeform modification. Best of both -- the model edits verified geometry
    # rather than inventing it.
    TEMPLATE_SEED = "template_seed"
    FREEFORM = "freeform"


class RegistryError(Exception):
    """A template is malformed. Always a registry bug, never a user error."""


@dataclass(frozen=True)
class PrintTest:
    """The physical-print status of a template.

    The difference between "this validates" and "this prints" is the entire
    product (spec section 13.3), so the distinction is recorded explicitly
    rather than implied. `status` is one of:

        untested  designed and validated, never physically printed
        passed    printed on the target machine, came out correct
        failed    printed and did not work; `rationale` says how

    A template defaults to `untested`, and nothing in the system may present an
    untested template as print-tested. The target machine and material are the
    configuration the defaults were chosen for, not a record of a test that
    happened; `rationale` explains why the values are what they are.
    """

    status: str = "untested"
    target_printer: str = ""
    target_material: str = "PLA"
    rationale: str = ""
    date: str = ""

    @property
    def passed(self) -> bool:
        return self.status.lower() == "passed"

    @property
    def verified(self) -> bool:
        return self.status.lower() in {"passed", "failed"}


@dataclass
class TextFeature:
    """Declares that a template renders text, and how to measure it.

    The DFM text checks need cap height, stroke width and relief depth. Rather
    than trying to recover those from a tessellated glyph, the template states
    which of its parameters carry them.
    """

    label_param: str | None = None
    cap_height_param: str | None = None
    depth_param: str | None = None
    # Stroke width as a fraction of cap height. 0.18 is about right for a
    # medium-weight sans-serif; bold runs nearer 0.22.
    stroke_ratio: float = 0.18

    def resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        cap = float(params.get(self.cap_height_param or "", 0) or 0)
        return {
            "label": str(params.get(self.label_param or "", "") or "text"),
            "cap_height_mm": cap,
            "depth_mm": float(params.get(self.depth_param or "", 0) or 0),
            "stroke_mm": cap * self.stroke_ratio,
        }


@dataclass
class Template:
    """One parametric product definition."""

    id: str
    version: int
    category: str
    display_name: str
    description: str
    language: str
    param_schema: dict[str, Any]
    source: str
    tags: list[str] = field(default_factory=list)
    embedding_text: str = ""
    # Postconditions: expressions over the *measured geometry*, checked after
    # the build.
    invariants: list[str] = field(default_factory=list)
    # Preconditions: expressions over the *parameters*, checked before it.
    #
    # The distinction is load-bearing. A JSON Schema constrains each parameter
    # independently, so it cannot say "the text must fit on the plate" -- that
    # is a relationship between two of them. Checking such a rule after the
    # build reports it as a validation failure, which reads as "the geometry is
    # broken" when the truth is "those two numbers cannot both be right". A
    # precondition rejects the combination up front with a message that names
    # the actual problem, and costs nothing to evaluate.
    preconditions: list[str] = field(default_factory=list)
    tested: PrintTest | None = None
    expected_solids: int = 1
    # Maps a schema parameter to the bbox axis it should control, so the
    # dimensional-fidelity check knows what "60 mm wide" refers to.
    dimension_map: dict[str, str] = field(default_factory=dict)
    text_features: list[TextFeature] = field(default_factory=list)
    notes: str = ""
    source_path: Path | None = None

    # -- parameters --------------------------------------------------------
    @property
    def properties(self) -> dict[str, Any]:
        props = self.param_schema.get("properties")
        return props if isinstance(props, dict) else {}

    @property
    def required(self) -> list[str]:
        req = self.param_schema.get("required")
        return list(req) if isinstance(req, list) else []

    def defaults(self) -> dict[str, Any]:
        """Every parameter's default value.

        A template whose defaults do not produce a valid model is a broken
        template, which is what the registry test suite asserts.
        """
        return {
            name: spec["default"]
            for name, spec in self.properties.items()
            if isinstance(spec, dict) and "default" in spec
        }

    def merge_params(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Caller parameters over defaults."""
        merged = self.defaults()
        merged.update(
            {k: v for k, v in (params or {}).items() if k in self.properties}
        )
        return merged

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Check parameters against the schema and the template's preconditions.

        Returns readable problems, empty when the parameters are usable.
        """
        return self._validate_schema(params) + self.check_preconditions(params)

    def check_preconditions(self, params: dict[str, Any]) -> list[str]:
        """Evaluate the cross-parameter constraints.

        A malformed precondition is reported as a problem rather than swallowed:
        a registry entry whose guard silently stops being checked is worse than
        one that fails loudly during review.
        """
        if not self.preconditions:
            return []
        from .validation.invariants import InvariantError, evaluate  # noqa: PLC0415

        merged = self.merge_params(params)
        problems: list[str] = []
        for expression in self.preconditions:
            try:
                if not evaluate(expression, dict(merged)):
                    problems.append(
                        f"these values do not satisfy the template's requirement "
                        f"`{expression}`"
                    )
            except InvariantError as exc:
                problems.append(f"precondition `{expression}` could not be evaluated: {exc}")
        return problems

    def _validate_schema(self, params: dict[str, Any]) -> list[str]:
        """Type, range and enum, via jsonschema when it is installed.

        The fallback exists because a parameter silently escaping its tested
        range is exactly how a validated template starts producing unprintable
        parts.
        """
        try:
            import jsonschema  # noqa: PLC0415

            validator = jsonschema.Draft202012Validator(self.param_schema)
            return [
                f"{'.'.join(str(p) for p in e.path) or 'params'}: {e.message}"
                for e in sorted(validator.iter_errors(params), key=lambda e: list(e.path))
            ]
        except ImportError:
            return self._validate_params_basic(params)

    def _validate_params_basic(self, params: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        for name in self.required:
            if name not in params:
                problems.append(f"{name}: required parameter is missing")
        for name, value in params.items():
            spec = self.properties.get(name)
            if not isinstance(spec, dict):
                continue
            problems.extend(_check_one(name, value, spec))
        return problems

    # -- source ------------------------------------------------------------
    def render_source(self, params: dict[str, Any] | None = None) -> str:
        """The template's source with parameters bound as constants."""
        merged = self.merge_params(params)
        return binding.bound_source(self.source, merged)

    def resolve_text_features(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [feature.resolve(params) for feature in self.text_features]

    def requested_dimensions(self, params: dict[str, Any]) -> dict[str, float]:
        """Translate template parameters into the axis targets to check against."""
        out: dict[str, float] = {}
        for param, axis in self.dimension_map.items():
            value = params.get(param)
            if isinstance(value, (int, float)):
                out[f"{axis}_mm"] = float(value)
        return out

    # -- search ------------------------------------------------------------
    @property
    def search_text(self) -> str:
        parts = [
            self.embedding_text,
            self.display_name,
            self.description,
            " ".join(self.tags),
            self.category,
        ]
        return " ".join(p for p in parts if p)

    # -- serialisation -----------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Compact form for a tool result or a listing.

        Terse on purpose: a `list_templates` result carrying forty full schemas
        would crowd out the conversation it is meant to inform.
        """
        return {
            "id": self.id,
            "version": self.version,
            "category": self.category,
            "display_name": self.display_name,
            "description": self.description.strip(),
            "tags": self.tags,
            "print_tested": bool(self.tested and self.tested.passed),
            "print_test_status": self.tested.status if self.tested else "unknown",
        }

    def detail(self) -> dict[str, Any]:
        payload = self.summary()
        payload.update(
            {
                "language": self.language,
                "param_schema": self.param_schema,
                "invariants": self.invariants,
                "requirements": self.preconditions,
                "defaults": self.defaults(),
            }
        )
        if self.tested:
            payload["print_test"] = {
                "status": self.tested.status,
                "target_printer": self.tested.target_printer,
                "target_material": self.tested.target_material,
                "rationale": self.tested.rationale.strip(),
            }
        if self.notes:
            payload["notes"] = self.notes
        return payload


def _check_one(name: str, value: Any, spec: dict[str, Any]) -> list[str]:
    """Type, range and enum checks for one parameter."""
    problems: list[str] = []
    expected = spec.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []

    if types and not _type_matches(value, types):
        problems.append(f"{name}: expected {' or '.join(str(t) for t in types)}")
        return problems

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and value < minimum:
            problems.append(
                f"{name}: {value} is below the minimum of {minimum}"
                + (f" ({spec['description']})" if spec.get("description") else "")
            )
        if maximum is not None and value > maximum:
            problems.append(f"{name}: {value} is above the maximum of {maximum}")

    if isinstance(value, str):
        max_len = spec.get("maxLength")
        if max_len is not None and len(value) > max_len:
            problems.append(f"{name}: longer than the {max_len} character maximum")

    choices = spec.get("enum")
    if choices and value not in choices:
        problems.append(f"{name}: {value!r} is not one of {choices}")

    return problems


def _type_matches(value: Any, types: list[Any]) -> bool:
    for type_name in types:
        if type_name == "null" and value is None:
            return True
        if type_name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if type_name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if type_name == "string" and isinstance(value, str):
            return True
        if type_name == "boolean" and isinstance(value, bool):
            return True
        if type_name == "array" and isinstance(value, list):
            return True
        if type_name == "object" and isinstance(value, dict):
            return True
    return False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class Matcher(Protocol):
    """Scores a query against a template. Returns 0..1, higher is better."""

    def score(self, query: str, template: Template) -> float: ...

    @property
    def thresholds(self) -> tuple[float, float]:
        """(template_threshold, seed_threshold) calibrated for this backend."""
        ...


class LexicalMatcher:
    """Token-overlap scoring with inverse document frequency weighting.

    Not as good as an embedding search at matching "pot for a plant on my wall"
    to a half-moon planter, and it does not pretend to be. It exists so the
    system has a working default with no model dependency, and so the registry
    can be tested deterministically. Swap in `EmbeddingMatcher` in production.

    Thresholds are lower than the spec's cosine numbers because a lexical score
    is sparse: a strong lexical match rarely exceeds 0.5, where a strong cosine
    match exceeds 0.85. Comparing them directly would route everything to
    freeform.
    """

    def __init__(self, templates: list[Template] | None = None):
        self._idf: dict[str, float] = {}
        if templates:
            self.fit(templates)

    @property
    def thresholds(self) -> tuple[float, float]:
        return (0.34, 0.18)

    def fit(self, templates: list[Template]) -> None:
        """Compute inverse document frequency across the registry.

        Without it, "wall" scores as highly as "gridfinity" -- and "wall"
        appears in a third of the registry while "gridfinity" identifies exactly
        one template.
        """
        total = max(1, len(templates))
        counts: dict[str, int] = {}
        for template in templates:
            for token in set(_tokenize(template.search_text)):
                counts[token] = counts.get(token, 0) + 1
        self._idf = {
            token: math.log((total + 1) / (count + 0.5))
            for token, count in counts.items()
        }

    def score(self, query: str, template: Template) -> float:
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return 0.0
        template_tokens = set(_tokenize(template.search_text))
        if not template_tokens:
            return 0.0

        default_idf = math.log(2.0)
        overlap = query_tokens & template_tokens
        matched = sum(self._idf.get(t, default_idf) for t in overlap)
        possible = sum(self._idf.get(t, default_idf) for t in query_tokens)
        if possible <= 0:
            return 0.0

        base = matched / possible
        # A category word in the query is strong evidence, so it is worth more
        # than its share of the token overlap.
        if template.category.replace("_", " ") in query.lower():
            base = min(1.0, base + 0.15)
        return round(base, 4)


class EmbeddingMatcher:
    """Cosine similarity over precomputed embeddings.

    The production matcher. Takes an `embed` callable so the registry does not
    depend on any particular provider; in the deployed system these vectors live
    in the `templates.embedding` pgvector column and the search happens in
    Postgres rather than here (spec section 11).
    """

    def __init__(self, embed, templates: list[Template] | None = None):
        self.embed = embed
        self._vectors: dict[str, list[float]] = {}
        if templates:
            self.fit(templates)

    @property
    def thresholds(self) -> tuple[float, float]:
        return (0.82, 0.70)

    def fit(self, templates: list[Template]) -> None:
        for template in templates:
            self._vectors[template.id] = self.embed(template.search_text)

    def score(self, query: str, template: Template) -> float:
        vector = self._vectors.get(template.id)
        if vector is None:
            vector = self.embed(template.search_text)
            self._vectors[template.id] = vector
        return _cosine(self.embed(query), vector)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


_STOPWORDS = frozenset(
    {
        "a", "an", "the", "for", "with", "and", "or", "of", "to", "in", "on",
        "my", "me", "i", "that", "this", "it", "is", "be", "can", "you", "make",
        "create", "design", "generate", "want", "need", "please", "some", "at",
        "as", "by", "from", "into", "about", "would", "like", "print", "printed",
    }
)

# Plural and comparative endings, longest first so "planters" -> "planter"
# rather than "planters" -> "planter" + a stray "s".
_SUFFIXES = ("ers", "ing", "es", "s")


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-letters, drop stopwords, crudely stem."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    tokens: list[str] = []
    for word in words:
        if word in _STOPWORDS or len(word) < 2:
            continue
        tokens.append(_stem(word))
    return tokens


def _stem(word: str) -> str:
    if len(word) <= 4:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


@dataclass
class Match:
    """One scored template, with the route the score implies."""

    template: Template
    score: float
    route: Route

    def as_dict(self) -> dict[str, Any]:
        payload = self.template.summary()
        payload["score"] = round(self.score, 4)
        payload["route"] = self.route.value
        return payload


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TemplateRegistry:
    """Loads, validates and searches the template collection."""

    def __init__(self, templates: list[Template] | None = None, matcher: Matcher | None = None):
        self._templates: dict[str, Template] = {}
        for template in templates or []:
            self._templates[template.id] = template
        self.matcher: Matcher = matcher or LexicalMatcher(list(self._templates.values()))

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(
        cls,
        directory: Path | str | None = None,
        matcher: Matcher | None = None,
        *,
        strict: bool = True,
    ) -> "TemplateRegistry":
        """Load every template YAML in a directory.

        With `strict=True` a malformed template aborts the load. That is
        deliberate: a registry that silently drops a broken entry will happily
        serve a catalogue missing the template a user is asking for, and nobody
        finds out until a support ticket.
        """
        path = Path(directory or TEMPLATE_DIR)
        templates: list[Template] = []
        errors: list[str] = []
        for file in sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")):
            try:
                templates.append(load_template_file(file))
            except Exception as exc:
                errors.append(f"{file.name}: {exc}")
        if errors and strict:
            raise RegistryError(
                "failed to load templates:\n  " + "\n  ".join(errors)
            )
        registry = cls(templates, matcher)
        registry.load_errors = errors  # type: ignore[attr-defined]
        return registry

    # -- access ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._templates)

    def __contains__(self, template_id: object) -> bool:
        return template_id in self._templates

    def get(self, template_id: str) -> Template:
        try:
            return self._templates[template_id]
        except KeyError:
            raise KeyError(
                f"no template {template_id!r}; "
                f"known templates: {', '.join(sorted(self._templates))}"
            ) from None

    def all(self) -> list[Template]:
        return sorted(self._templates.values(), key=lambda t: (t.category, t.id))

    def list(self, category: str | None = None) -> list[Template]:
        if not category:
            return self.all()
        return [t for t in self.all() if t.category == category]

    def categories(self) -> list[str]:
        return sorted({t.category for t in self._templates.values()})

    # -- search ------------------------------------------------------------
    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[Match]:
        """Rank templates against a query, best first."""
        candidates = self.list(category)
        template_threshold, seed_threshold = self.matcher.thresholds
        scored: list[Match] = []
        for template in candidates:
            score = self.matcher.score(query, template)
            scored.append(Match(template, score, _route_for(score, template_threshold, seed_threshold)))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def best_match(self, query: str, category: str | None = None) -> Match | None:
        matches = self.search(query, category, limit=1)
        return matches[0] if matches else None

    def route(self, query: str, category: str | None = None) -> tuple[Route, Match | None]:
        """The generation path this query should take, and why."""
        match = self.best_match(query, category)
        if match is None:
            return Route.FREEFORM, None
        return match.route, match


def _route_for(score: float, template_threshold: float, seed_threshold: float) -> Route:
    if score >= template_threshold:
        return Route.TEMPLATE
    if score >= seed_threshold:
        return Route.TEMPLATE_SEED
    return Route.FREEFORM


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("id", "category", "display_name", "description", "language", "source")


def load_template_file(path: Path | str) -> Template:
    """Parse and validate one template YAML file."""
    file = Path(path)
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RegistryError("template file must contain a YAML mapping")
    template = parse_template(raw)
    template.source_path = file
    return template


def parse_template(raw: dict[str, Any]) -> Template:
    """Build a Template from a parsed mapping, checking every invariant."""
    missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise RegistryError(f"missing required field(s): {', '.join(missing)}")

    param_schema = raw.get("param_schema") or {"type": "object", "properties": {}}
    if not isinstance(param_schema, dict):
        raise RegistryError("param_schema must be a mapping")

    source = raw["source"]
    language = raw["language"]

    if language in {"build123d", "cadquery"}:
        _check_template_source(source, param_schema)

    test_raw = raw.get("print_test")
    tested = None
    if isinstance(test_raw, dict):
        status = str(test_raw.get("status", "untested")).lower()
        if status not in {"untested", "passed", "failed"}:
            raise RegistryError(
                f"print_test.status must be untested, passed or failed; got {status!r}"
            )
        tested = PrintTest(
            status=status,
            target_printer=str(test_raw.get("target_printer", "")),
            target_material=str(test_raw.get("target_material", "PLA")),
            rationale=str(test_raw.get("rationale", "")),
            date=str(test_raw.get("date", "")),
        )

    text_features = [
        TextFeature(
            label_param=item.get("label_param"),
            cap_height_param=item.get("cap_height_param"),
            depth_param=item.get("depth_param"),
            stroke_ratio=float(item.get("stroke_ratio", 0.18)),
        )
        for item in raw.get("text_features") or []
        if isinstance(item, dict)
    ]

    return Template(
        id=str(raw["id"]),
        version=int(raw.get("version", 1)),
        category=str(raw["category"]),
        display_name=str(raw["display_name"]),
        description=str(raw["description"]).strip(),
        language=str(language),
        param_schema=param_schema,
        source=str(source),
        tags=[str(t) for t in raw.get("tags") or []],
        embedding_text=str(raw.get("embedding_text", "")),
        invariants=[str(i) for i in raw.get("invariants") or []],
        preconditions=[str(i) for i in raw.get("preconditions") or []],
        tested=tested,
        expected_solids=int(raw.get("expected_solids", 1)),
        dimension_map=dict(raw.get("dimension_map") or {}),
        text_features=text_features,
        notes=str(raw.get("notes", "")),
    )


def _check_template_source(source: str, param_schema: dict[str, Any]) -> None:
    """Static checks a template must satisfy to be loadable.

    Templates are hand-authored and reviewed, so the magic-number style rule is
    relaxed for them -- but every schema parameter must have a matching
    module-level constant, or binding silently does nothing and the template
    ignores the user's dimensions entirely. That failure is invisible at every
    other layer, which is why it is caught at load time.
    """
    scan = security.scan(source, enforce_named_constants=False)
    if not scan.ok:
        raise RegistryError(
            "source failed the static gate:\n    "
            + "\n    ".join(str(v) for v in scan.errors)
        )

    declared = binding.declared_constants(source)
    properties = param_schema.get("properties") or {}
    unbindable = [
        name
        for name in properties
        if binding.constant_name(name) not in declared
    ]
    if unbindable:
        raise RegistryError(
            "these parameters have no matching module-level constant, so setting "
            "them would have no effect: "
            + ", ".join(f"{n} (expected {binding.constant_name(n)})" for n in sorted(unbindable))
        )
