"""Pre-generation content policy (spec section 10.1).

Two independent concerns, both checked before any geometry is produced:

* **Intellectual property.** Users print and sell what this system makes. A
  Mickey Mouse keychain is a real commercial liability for the operator and for
  the seller, and "the AI made it" is not a defence. This is the single most
  likely way a product like this attracts a lawsuit.
* **Safety.** Weapon components and restricted parts are refused. "It's just a
  printable file" is not a defence either.

The classifier here is a keyword and pattern matcher, deliberately. It is fast,
free, auditable, and it runs on every request before a model is called. It is
*not* sufficient on its own -- it will miss paraphrases and obfuscation, and a
model-based classifier should back it up on the same input (`review_with_model`
is where that plugs in). Treating this as the whole control would be a mistake;
treating it as the cheap first pass that catches the obvious cases is correct.

Deliberately not blocked: generic shapes that happen to share a word with a
protected term. Refusing "mouse pad holder" because of "mouse" would be both
useless and infuriating, so matches require the distinctive part of a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    ALLOW = "allow"
    # Proceed, but record it: a borderline request worth reviewing in
    # aggregate, not worth blocking one user over.
    FLAG = "flag"
    REFUSE = "refuse"


@dataclass
class PolicyResult:
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    category: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is not Decision.REFUSE

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "category": self.category,
            "reasons": self.reasons,
            "matched": self.matched,
        }

    def user_message(self) -> str:
        """What to tell the user. Specific about what, vague about how."""
        if self.decision is not Decision.REFUSE:
            return ""
        if self.category == "ip":
            return (
                "This request names a character, logo or brand that is likely "
                "protected by copyright or trademark, so FormForge will not "
                "generate it. Describing the shape you want in generic terms -- "
                "the form, the proportions, the style -- will usually get you a "
                "model you can actually print and sell."
            )
        if self.category == "weapon":
            return (
                "FormForge does not generate firearm components or other "
                "regulated weapon parts."
            )
        return "FormForge will not generate this request."


# Distinctive names only. A term that is also an ordinary English word in a
# maker context ("mouse", "shield", "star") is not on this list on its own --
# it appears only as part of a longer distinctive phrase.
_PROTECTED_CHARACTERS = (
    r"mickey\s*mouse", r"minnie\s*mouse", r"donald\s*duck", r"\bgoofy\b",
    r"winnie[\s-]*the[\s-]*pooh", r"\bpikachu\b", r"\bcharizard\b", r"\bpokemon\b",
    r"\bpok[ée]ball\b", r"hello\s*kitty", r"super\s*mario", r"\bluigi\b",
    r"\byoshi\b", r"\bbowser\b", r"\bzelda\b", r"\bsonic\s+the\s+hedgehog\b",
    r"\bspider[\s-]*man\b", r"\bbatman\b", r"\bsuperman\b", r"\bironman\b",
    r"iron\s*man", r"\bhulk\b", r"\bthor\b", r"captain\s*america",
    r"\bavengers\b", r"\bdarth\s*vader\b", r"\bstormtrooper\b", r"\bbaby\s*yoda\b",
    r"\bgrogu\b", r"\bmandalorian\b", r"\bstar\s*wars\b", r"\bmillennium\s*falcon\b",
    r"\bharry\s*potter\b", r"\bhogwarts\b", r"\bminion(s)?\b", r"\bshrek\b",
    r"\belsa\b.*\bfrozen\b", r"\bstitch\b.*\blilo\b", r"\bbluey\b",
    r"\bpeppa\s*pig\b", r"\bpaw\s*patrol\b", r"\bthomas\s+the\s+tank\b",
    r"\bgudetama\b", r"\btotoro\b", r"\bnaruto\b", r"\bgoku\b", r"\bdragon\s*ball\b",
    r"\bmine\s*craft\b", r"\bminecraft\b", r"\bcreeper\b.*\bminecraft\b",
    r"\bfortnite\b", r"\bamong\s*us\b.*\bcrewmate\b", r"\bbaby\s*shark\b",
)

_PROTECTED_BRANDS = (
    r"\bnike\b", r"\bswoosh\b", r"\badidas\b", r"\bgucci\b", r"\blouis\s*vuitton\b",
    r"\bchanel\b", r"\bsupreme\b(?=.*\blogo\b)", r"\bferrari\b", r"\blamborghini\b",
    r"\bporsche\b", r"\bbmw\b", r"\bmercedes\b", r"\btesla\b(?=.*\blogo\b)",
    r"\bapple\s*logo\b", r"\bstarbucks\b", r"\bcoca[\s-]*cola\b", r"\bmcdonald'?s\b",
    r"\bgolden\s*arches\b", r"\bplaystation\b", r"\bxbox\b", r"\bnintendo\b",
    r"\blego\b", r"\bnfl\b", r"\bnba\b", r"\bfifa\b", r"\bolympic\s*rings\b",
    r"\bdisney\b", r"\bpixar\b", r"\bmarvel\b", r"\bnetflix\s*logo\b",
)

_WEAPON_TERMS = (
    r"\bfirearm\b", r"\bhandgun\b", r"\bpistol\b", r"\brifle\b", r"\bshotgun\b",
    r"\bglock\b", r"\bar[\s-]*15\b", r"\bak[\s-]*47\b",
    r"\blower\s*receiver\b", r"\bupper\s*receiver\b", r"\btrigger\s*group\b",
    r"\bsear\b", r"\bauto\s*sear\b", r"\bbump\s*stock\b", r"\bsuppressor\b",
    r"\bsilencer\b", r"\bmagazine\s*(?:for|extension)\b.*\b(?:gun|rifle|pistol)\b",
    r"\bhigh[\s-]*capacity\s*magazine\b", r"\bghost\s*gun\b", r"\bzip\s*gun\b",
    r"\bfgc[\s-]*9\b", r"\bliberator\s*pistol\b",
    r"\bbrass\s*knuckle", r"\bknuckle\s*duster\b", r"\bthrowing\s*star\b",
    r"\bshuriken\b", r"\bswitchblade\b", r"\bbutterfly\s*knife\b",
    r"\bgrenade\b", r"\bexplosive\b", r"\bdetonator\b", r"\bsilenced\b.*\bbarrel\b",
)

# Ambiguous terms: a plausible innocent reading exists, so these are flagged for
# review rather than refused. A "knife block" is a kitchen organizer.
_FLAG_TERMS = (
    r"\bknife\b", r"\bblade\b", r"\bdagger\b", r"\bsword\b", r"\bcrossbow\b",
    r"\bbolt\s*carrier\b", r"\bpicklock\b", r"\block\s*pick\b", r"\bbump\s*key\b",
    r"\bskimmer\b", r"\bcard\s*reader\s*(?:shim|overlay)\b",
)

_COMPILED_IP = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in _PROTECTED_CHARACTERS + _PROTECTED_BRANDS
)
_COMPILED_WEAPON = tuple(re.compile(p, re.IGNORECASE) for p in _WEAPON_TERMS)
_COMPILED_FLAG = tuple(re.compile(p, re.IGNORECASE) for p in _FLAG_TERMS)


def classify(prompt: str, *, text_content: str | None = None) -> PolicyResult:
    """Screen a request before any geometry is generated.

    `text_content` is checked separately and just as hard: "put NIKE on it" is
    the same problem as "make a Nike keychain", and a system that only screened
    the shape description would miss every one of them.
    """
    haystack = " ".join(part for part in (prompt, text_content) if part)
    if not haystack.strip():
        return PolicyResult(Decision.ALLOW)

    weapon_hits = [p.pattern for p in _COMPILED_WEAPON if p.search(haystack)]
    if weapon_hits:
        return PolicyResult(
            decision=Decision.REFUSE,
            category="weapon",
            reasons=["the request names a firearm component or regulated weapon part"],
            matched=weapon_hits,
        )

    ip_hits = [p.pattern for p in _COMPILED_IP if p.search(haystack)]
    if ip_hits:
        return PolicyResult(
            decision=Decision.REFUSE,
            category="ip",
            reasons=[
                "the request names a character, logo or brand that is likely "
                "protected by copyright or trademark"
            ],
            matched=ip_hits,
        )

    flag_hits = [p.pattern for p in _COMPILED_FLAG if p.search(haystack)]
    if flag_hits:
        return PolicyResult(
            decision=Decision.FLAG,
            category="review",
            reasons=[
                "the request contains a term with both an innocent and a "
                "restricted reading; allowed, but recorded for review"
            ],
            matched=flag_hits,
        )

    return PolicyResult(Decision.ALLOW)


REVIEW_SYSTEM_PROMPT = """\
You screen 3D-printing requests before a model is generated. Decide whether the \
request asks for either:

1. A recognisable copyrighted character, trademarked logo, or brand-identifying \
design that the user could not lawfully print and sell.
2. A firearm component, weapon part, or other regulated item.

Generic shapes, styles and descriptions are fine even when they resemble a \
commercial product: "a rounded cartoon animal keychain" is allowed, "a Pikachu \
keychain" is not. A kitchen knife block is allowed; a knife blade is not.

Respond with a JSON object only:
{"decision": "allow" | "refuse", "category": "ip" | "weapon" | null, \
"reason": "<one sentence>"}"""


def review_with_model(prompt: str, client, *, text_content: str | None = None) -> PolicyResult:
    """Second-pass screening by a model, for what the patterns miss.

    Runs on the fast tier: it is one short call on every request, so it has to
    be cheap. A model failure here allows the request -- the pattern pass has
    already run and blocking every generation because a classifier call timed
    out is the wrong trade.
    """
    from .llm import Tier  # noqa: PLC0415 -- avoids a circular import

    if not getattr(client, "available", False):
        return PolicyResult(Decision.ALLOW, reasons=["model review unavailable"])

    request = prompt if not text_content else f"{prompt}\n\nText on the model: {text_content}"
    try:
        completion = client.complete(
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": request}],
            tier=Tier.FAST,
            max_tokens=256,
            effort="low",
            purpose="content policy review",
        )
    except Exception:
        return PolicyResult(Decision.ALLOW, reasons=["model review failed; pattern pass applied"])

    payload = completion.json_block() or {}
    if str(payload.get("decision", "allow")).lower() == "refuse":
        return PolicyResult(
            decision=Decision.REFUSE,
            category=payload.get("category") or "ip",
            reasons=[str(payload.get("reason", "refused by content policy review"))],
            matched=["model_review"],
        )
    return PolicyResult(Decision.ALLOW)


def screen(prompt: str, client=None, *, text_content: str | None = None) -> PolicyResult:
    """Full screening: patterns first, then the model if one is available."""
    result = classify(prompt, text_content=text_content)
    if result.decision is Decision.REFUSE or client is None:
        return result
    model_result = review_with_model(prompt, client, text_content=text_content)
    if model_result.decision is Decision.REFUSE:
        return model_result
    return result
