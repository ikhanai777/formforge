#!/usr/bin/env python3
"""
webprobe -- capture everything a web page does, so you can write a scraper for it.

This is a study tool. Point it at a URL and it records the whole request/response
surface of the page -- the HTML before JavaScript runs, the DOM after, every network
call the browser made (headers, POST bodies, response bodies), the embedded state
blobs, cookies and storage, the console, the screenshots -- and then analyses that
capture into the handful of facts you actually need in order to write a scraper:

  * which framework/CDN/WAF the site runs on,
  * which anti-bot vendor is in the path (if any),
  * which first-party JSON endpoints exist, what parameters they take, what their
    responses look like, and -- crucially -- whether they still answer when replayed
    from plain Python without a browser,
  * where the listing records live (in a JSON payload, in an embedded __NEXT_DATA__
    style blob, or only in the rendered DOM),
  * candidate CSS selectors for the repeated blocks in the DOM,
  * how pagination is expressed,
  * what robots.txt says and what crawl delay to use.

It writes three reports: report.json (machine readable), REPORT.md (for you), and
HERMES_BRIEF.md (a self-contained prompt to hand to a coding agent along with this
directory, so it can write the scraper).

Usage
-----
    # full capture of one page
    python3 webprobe.py https://www.dubizzle.com/motors/used-cars/

    # a listing page and a detail page in one run (what a scraper actually needs)
    python3 webprobe.py https://site.com/search https://site.com/item/123 \
        --scroll 5 --goal "all car listings with price, title, mileage, url"

    # no browser, stdlib only -- works anywhere, sees only server-rendered HTML
    python3 webprobe.py https://site.com --static

Install (only needed for the browser capture)
---------------------------------------------
    pip install playwright && playwright install chromium

Etiquette
---------
robots.txt is honoured by default for the pages you name; pass --ignore-robots to
override that decision yourself. Requests are throttled and each page is fetched
once. Keep it that way: this tool is for understanding a site, not for hammering it.
Captured headers and cookies are your own session credentials -- pass --redact
before sharing an output directory with anyone, including an agent.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "1.0.0"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

TEXT_MIMES = (
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/graphql",
    "application/xml",
    "application/ld+json",
    "application/manifest+json",
    "text/",
    "+json",
    "+xml",
)

# Public-suffix-lite. Enough to tell "api.dubizzle.com is us" from "gstatic.com is not"
# without pulling in a dependency for a two-line question.
TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "com.au", "net.au", "org.au",
    "co.nz", "co.za", "com.br", "com.mx", "com.ar", "com.tr", "com.sa", "com.eg",
    "com.pk", "co.in", "com.sg", "com.hk", "com.tw", "com.cn", "co.kr", "com.my",
    "co.id", "com.ph", "com.ua", "co.il", "com.ng", "com.gh", "com.ke", "com.vn",
    "com.pe", "com.co", "com.ve", "com.ec", "com.uy", "com.bo", "com.do", "com.pa",
    "com.gt", "com.sv", "com.hn", "com.ni", "com.cr", "com.pr", "com.cy", "com.mt",
    "com.qa", "com.kw", "com.bh", "com.om", "com.jo", "com.lb", "com.ma", "com.tn",
    "com.dz", "com.lk", "com.bd", "com.np", "co.th", "or.jp", "ne.jp",
}

# name -> list of (evidence_source, regex). Evidence sources: header, cookie, url,
# html, global.
STACK_SIGNS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Next.js", [("html", r"__NEXT_DATA__|/_next/static/"), ("url", r"/_next/(static|data|image)"),
                 ("header", r"x-nextjs-|x-powered-by:\s*next")]),
    ("Nuxt/Vue SSR", [("html", r"__NUXT__|/_nuxt/"), ("url", r"/_nuxt/"), ("global", r"__NUXT__")]),
    ("React", [("html", r"data-reactroot|data-reactid|__REACT_DEVTOOLS"),
               ("url", r"react(-dom)?[.@-][\d.]*(production|min)"), ("global", r"^React$")]),
    ("Vue", [("html", r"\sdata-v-[0-9a-f]{6,}|__vue__"), ("global", r"^Vue$")]),
    ("Angular", [("html", r"ng-version=|ng-app=|_nghost-|_ngcontent-"), ("url", r"zone\.js|/polyfills\.[0-9a-f]+\.js"),
                 ("global", r"^(ng|angular)$")]),
    ("SvelteKit", [("html", r"svelte-[0-9a-z]{6,}|__sveltekit"), ("url", r"/_app/immutable/")]),
    ("Remix", [("html", r"__remixContext|__remixManifest")]),
    ("Astro", [("html", r"astro-island|astro-slot")]),
    ("Gatsby", [("html", r"___gatsby"), ("url", r"/page-data/.*/page-data\.json")]),
    ("Apollo / GraphQL client", [("html", r"__APOLLO_STATE__|apolloClient"), ("url", r"/graphql")]),
    ("Redux store in HTML", [("html", r"__INITIAL_STATE__|__PRELOADED_STATE__|__REDUX")]),
    ("jQuery", [("url", r"jquery[.\-/][\d.]*(min)?\.js"), ("global", r"^jQuery$")]),
    ("WordPress", [("html", r"/wp-content/|/wp-includes/|wp-json"), ("url", r"/wp-json/|/wp-content/")]),
    ("Drupal", [("header", r"x-drupal|x-generator:\s*drupal"), ("html", r"drupal-settings-json")]),
    ("Shopify", [("header", r"x-shopid|x-shopify"), ("url", r"cdn\.shopify\.com|/cart/add"),
                 ("global", r"^Shopify$")]),
    ("Magento", [("html", r"Magento_|mage/cookies"), ("cookie", r"^(mage-|form_key)")]),
    ("Salesforce Commerce", [("url", r"demandware|/on/demandware\.store/")]),
    ("Algolia search", [("url", r"algolia(net|nia)?\.(net|com)"), ("header", r"x-algolia-")]),
    ("Elasticsearch/OpenSearch API", [("url", r"/_search(\?|$)|/_msearch")]),
    ("Typesense", [("url", r"typesense")]),
    ("Cloudinary/Imgix media", [("url", r"res\.cloudinary\.com|imgix\.net")]),
    ("AWS CloudFront", [("header", r"x-amz-cf-id|via:.*cloudfront")]),
    ("Fastly", [("header", r"x-served-by:.*cache|x-fastly|fastly-")]),
    ("Vercel", [("header", r"x-vercel-id|server:\s*vercel")]),
    ("Netlify", [("header", r"x-nf-request-id|server:\s*netlify")]),
    ("nginx", [("header", r"server:\s*nginx")]),
    ("Apache", [("header", r"server:\s*apache")]),
    ("Varnish", [("header", r"x-varnish|via:.*varnish")]),
    ("Google Tag Manager", [("url", r"googletagmanager\.com")]),
    ("Segment", [("url", r"cdn\.segment\.(com|io)")]),
    ("Sentry", [("url", r"sentry\.io|browser\.sentry-cdn")]),
]

ANTIBOT_SIGNS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("Cloudflare (CDN/WAF)", "medium",
     [("header", r"server:\s*cloudflare|cf-ray:|cf-cache-status:"), ("cookie", r"^(__cf_bm|cf_clearance|__cfruid)"),
      ("url", r"/cdn-cgi/")]),
    ("Cloudflare Bot Management / Turnstile", "high",
     [("url", r"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform"),
      ("html", r"cf-turnstile|Checking if the site connection is secure")]),
    ("Akamai Bot Manager", "high",
     [("cookie", r"^(_abck|bm_sz|ak_bmsc|bm_sv|_bm)"), ("url", r"/akam/\d|akamai"), ("global", r"^bmak$")]),
    ("DataDome", "high",
     [("cookie", r"^datadome"), ("url", r"datadome\.co|js\.datadome"), ("header", r"x-datadome")]),
    ("PerimeterX / HUMAN", "high",
     [("cookie", r"^_px"), ("url", r"px-cdn|px-cloud\.net|perimeterx"), ("global", r"^_px")]),
    ("Imperva/Incapsula", "high",
     [("cookie", r"^(visid_incap|incap_ses|nlbi_)"), ("url", r"_Incapsula_Resource"),
      ("header", r"x-iinfo|x-cdn:\s*incapsula")]),
    ("Kasada", "high", [("url", r"/149e9513-01fa-4fb0-aad4-566afd725d1b/|kasada"), ("header", r"x-kpsdk")]),
    ("F5 Shape / Distil", "high", [("cookie", r"^(_imp_apg_r_|D_)"), ("url", r"distilnetworks|shieldsquare")]),
    ("Queue-it", "medium", [("url", r"queue-it\.net"), ("cookie", r"^Queue-it")]),
    ("reCAPTCHA", "high", [("url", r"google\.com/recaptcha|gstatic\.com/recaptcha")]),
    ("hCaptcha", "high", [("url", r"hcaptcha\.com")]),
    ("FingerprintJS", "medium", [("url", r"fpjs|fingerprintjs")]),
    ("AWS WAF", "medium", [("cookie", r"^aws-waf"), ("url", r"token\.awswaf\.com")]),
    ("Sucuri", "medium", [("header", r"x-sucuri-id")]),
]

# Third-party noise. These are real endpoints, but they belong to somebody else's
# analytics and are never what you want to scrape, so they get folded away.
TRACKER_HOSTS = re.compile(
    r"(google-analytics|googletagmanager|googleadservices|googlesyndication|doubleclick|"
    r"g\.doubleclick|analytics\.google|facebook\.(com|net)|connect\.facebook|hotjar|"
    r"sentry\.io|ingest\.sentry|segment\.(com|io)|mixpanel|amplitude|newrelic|nr-data|"
    r"clarity\.ms|bat\.bing|tiktok(cdn|v)?\.com|criteo|taboola|outbrain|branch\.io|"
    r"appsflyer|adjust\.(com|io)|onesignal|intercom|zendesk|zdassets|optimizely|"
    r"cloudflareinsights|scorecardresearch|quantserve|adsrvr|rubiconproject|pubmatic|"
    r"casalemedia|openx|smartadserver|yieldmo|moatads|adnxs|snapchat|pinterest\.com/ct|"
    r"gemius|hs-analytics|hubspot|fullstory|logrocket|smartlook|inspectlet|mouseflow|"
    r"usercentrics|onetrust|cookielaw|iubenda|didomi|sourcepoint|cmp\.)",
    re.I,
)

AUTHISH_HEADERS = re.compile(
    r"^(authorization|cookie|x-api-key|apikey|api-key|x-auth[\w-]*|x-access-token|"
    r"x-csrf[\w-]*|x-xsrf[\w-]*|x-requested-with|x-algolia-[\w-]+|x-client-[\w-]+|"
    r"x-app-[\w-]+|x-device-[\w-]+|x-session[\w-]*|x-token|token|x-tenant[\w-]*|"
    r"ocp-apim-subscription-key|x-amz-security-token|x-shopify-[\w-]+|x-build[\w-]*|"
    r"x-platform|x-locale|x-country|x-currency|x-signature|x-timestamp|x-nonce|"
    r"x-datadome[\w-]*|x-kpsdk[\w-]*)$",
    re.I,
)

PRICE_RE = re.compile(
    r"(?:(?:AED|USD|EUR|GBP|SAR|QAR|KWD|BHD|OMR|EGP|PKR|INR|TRY|ZAR|NGN|KES|RS|SR|DH)"
    r"\s*[\d][\d,.\s]{1,15}|[\d][\d,.\s]{1,15}\s*(?:AED|USD|EUR|GBP|SAR|QAR|KWD|EGP|PKR|"
    r"INR|TRY|ZAR|NGN|KES|درهم|ريال|جنيه)|[$€£₹₨]\s?[\d][\d,.]{1,15})",
    re.I,
)
DATEISH_RE = re.compile(
    r"(\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"\b(yesterday|today|just now|\d+\s+(minute|hour|day|week|month|year)s?\s+ago)\b)",
    re.I,
)
PAGINATION_PARAM_RE = re.compile(
    r"^(page|p|pg|pageno|page_number|page\[number\]|offset|start|from|skip|cursor|after|"
    r"next|limit|size|per_page|pagesize|page_size|hitsperpage|rows|count)$", re.I
)
ID_SEGMENT_RE = re.compile(
    r"^(\d+|[0-9a-f]{8,}|[0-9a-fA-F-]{32,}|[A-Za-z0-9_-]{22,})$"
)
NOISY_CLASS_RE = re.compile(
    r"^(_[\w-]{4,}|[a-z]{1,4}-[0-9a-z]{5,}|[\w]+__[0-9a-z]{5,}|[\w-]*[0-9a-f]{6,}[\w-]*|"
    r"css-[0-9a-z]+|sc-[0-9a-zA-Z]+|jsx-\d+|svelte-[0-9a-z]+)$"
)
UTILITY_CLASS_RE = re.compile(
    r"^(-?(m|p)[trblxy]?-|w-|h-|min-|max-|text-|bg-|border|rounded|flex|grid|gap-|"
    r"items-|justify-|self-|absolute|relative|fixed|sticky|hidden|block|inline|"
    r"truncate|overflow|z-\d|opacity|shadow|font-|leading-|tracking-|cursor-|"
    r"transition|transform|hover:|md:|lg:|sm:|xl:|col-|row-)"
)
STABLE_ATTRS = (
    "data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation",
    "data-component", "data-role", "data-name", "data-id", "itemprop", "itemtype",
    "aria-label", "role", "name",
)

CONSENT_TEXTS = (
    "accept all", "accept cookies", "accept", "agree", "i agree", "allow all", "got it",
    "ok, got it", "continue", "alle akzeptieren", "tout accepter", "aceptar", "موافق",
    "قبول", "الموافقة",
)
CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button#didomi-notice-agree-button",
    "button[aria-label*='accept' i]",
    "[data-testid*='accept' i]",
    "[id*='cookie'] button",
    ".cookie-consent button",
)

INTERESTING_GLOBALS = (
    "__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__",
    "__APOLLO_STATE__", "__remixContext", "__sveltekit", "___gatsby", "dataLayer",
    "__ENV__", "__CONFIG__", "__APP_CONFIG__", "__SERVER_DATA__", "__data",
    "Shopify", "wp", "drupalSettings", "algoliasearch", "React", "Vue", "ng", "jQuery",
    "bmak", "_px", "PerimeterX", "kpsdk", "google_tag_manager",
)


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str, quiet: bool = False, level: str = "..") -> None:
    if not quiet:
        print(f"  [{level}] {msg}", file=sys.stderr, flush=True)


def slugify(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (s[:limit] or "x").lower()


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in TWO_LABEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_texty(mime: str) -> bool:
    m = (mime or "").lower()
    return any(t in m for t in TEXT_MIMES)


def truncate(value, limit: int = 300):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...(+{len(value) - limit} chars)"
    return value


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def redact_value(name: str, value: str, redact: bool) -> str:
    if not redact or not value:
        return value
    if AUTHISH_HEADERS.match(name or "") and len(value) > 8:
        return value[:4] + "***REDACTED***"
    return value


def normalise_path(url: str) -> str:
    """/motors/used-cars/toyota/12345/ -> /motors/used-cars/toyota/{id}/"""
    parsed = urllib.parse.urlsplit(url)
    segs = []
    for seg in parsed.path.split("/"):
        segs.append("{id}" if seg and ID_SEGMENT_RE.match(seg) else seg)
    return "/".join(segs) or "/"


def flatten_headers(headers) -> list[str]:
    """HAR / mapping headers -> ["name: value", ...] for regex evidence scanning."""
    out = []
    if isinstance(headers, dict):
        items = headers.items()
    else:
        items = [(h.get("name", ""), h.get("value", "")) for h in (headers or [])]
    for name, value in items:
        out.append(f"{str(name).lower()}: {value}")
    return out


# --------------------------------------------------------------------------------------
# JSON shape inference -- "what does this endpoint return" in a form you can read
# --------------------------------------------------------------------------------------

def infer_shape(value, depth: int = 0, max_depth: int = 5):
    if depth >= max_depth:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key in list(value.keys())[:40]:
            out[key] = infer_shape(value[key], depth + 1, max_depth)
        if len(value) > 40:
            out["..."] = f"+{len(value) - 40} more keys"
        return out
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return [f"list[{len(value)}] of", infer_shape(value[0], depth + 1, max_depth)]
    if isinstance(value, bool):
        return f"bool ({value})"
    if isinstance(value, (int, float)):
        return f"number ({value})"
    if value is None:
        return "null"
    text = str(value)
    return f"str ({truncate(text, 60)})"


def find_record_arrays(value, path: str = "$", found=None, depth: int = 0):
    """Locate the arrays-of-similar-objects in a JSON payload: the listing rows."""
    if found is None:
        found = []
    if depth > 8 or len(found) > 12:
        return found
    if isinstance(value, list):
        dicts = [v for v in value[:5] if isinstance(v, dict)]
        if len(dicts) >= 2:
            keys = sorted(set(dicts[0].keys()) & set(dicts[1].keys()))
            if keys:
                found.append({
                    "path": path,
                    "count": len(value),
                    "shared_keys": keys[:40],
                    "sample": {k: truncate(dicts[0].get(k), 120) for k in keys[:25]},
                })
        for item in value[:2]:
            find_record_arrays(item, f"{path}[0]", found, depth + 1)
    elif isinstance(value, dict):
        for key, item in list(value.items())[:60]:
            find_record_arrays(item, f"{path}.{key}", found, depth + 1)
    return found


# --------------------------------------------------------------------------------------
# a very small DOM, so the static path needs nothing but the standard library
# --------------------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[Node] = []
        self.parent = parent
        self.text = ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs}>"

    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, tag: str | None = None, pred=None) -> list["Node"]:
        out = []
        for node in self.walk():
            if node is self:
                continue
            if tag and node.tag != tag:
                continue
            if pred and not pred(node):
                continue
            out.append(node)
        return out

    def inner_text(self, limit: int = 4000) -> str:
        chunks = []
        for node in self.walk():
            if node.tag in ("script", "style", "noscript", "template", "svg"):
                continue
            if node.text:
                chunks.append(node.text)
            if sum(len(c) for c in chunks) > limit:
                break
        return re.sub(r"\s+", " ", " ".join(chunks)).strip()[:limit]

    def own_text(self) -> str:
        return re.sub(r"\s+", " ", self.text).strip()

    def depth(self) -> int:
        depth, node = 0, self
        while node.parent is not None:
            depth += 1
            node = node.parent
        return depth


class MiniDOM(HTMLParser):
    """Forgiving tree builder. Not a browser, but enough to reason about structure."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.cursor = self.root
        self.scripts: list[dict] = []
        self._script_attrs: dict | None = None
        self._script_buf: list[str] = []
        try:
            self.feed(html)
        except Exception:  # a malformed page should not kill the run
            pass

    def handle_starttag(self, tag, attrs):
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs}
        node = Node(tag, attr_map, self.cursor)
        self.cursor.children.append(node)
        if tag == "script":
            self._script_attrs = attr_map
            self._script_buf = []
        if tag not in VOID_TAGS:
            self.cursor = node

    def handle_startendtag(self, tag, attrs):
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs}
        self.cursor.children.append(Node(tag, attr_map, self.cursor))

    def handle_endtag(self, tag):
        if tag == "script" and self._script_attrs is not None:
            self.scripts.append({
                "type": self._script_attrs.get("type", ""),
                "id": self._script_attrs.get("id", ""),
                "src": self._script_attrs.get("src", ""),
                "content": "".join(self._script_buf),
            })
            self._script_attrs = None
            self._script_buf = []
        node = self.cursor
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cursor = node.parent

    def handle_data(self, data):
        if self._script_attrs is not None:
            self._script_buf.append(data)
            return
        if data.strip():
            self.cursor.text += data


def class_quality(token: str) -> str:
    if NOISY_CLASS_RE.match(token):
        return "hashed"
    if UTILITY_CLASS_RE.match(token):
        return "utility"
    return "stable"


def stable_classes(node: Node) -> list[str]:
    return [c for c in node.classes() if class_quality(c) == "stable"][:3]


def stable_attr(node: Node) -> tuple[str, str] | None:
    for attr in STABLE_ATTRS:
        value = node.attrs.get(attr)
        if value and len(value) < 60:
            return attr, value
    return None


def css_for(node: Node, stop_at: Node | None = None) -> str:
    """A selector for `node`, relative to `stop_at`, preferring durable hooks."""
    parts: list[str] = []
    current: Node | None = node
    while current is not None and current is not stop_at and current.tag != "#document":
        attr = stable_attr(current)
        if attr and attr[0] != "role":
            parts.append(f'{current.tag}[{attr[0]}="{attr[1]}"]')
            break
        node_id = current.attrs.get("id", "")
        if node_id and class_quality(node_id) == "stable" and not node_id[:1].isdigit():
            parts.append(f"#{node_id}")
            break
        classes = stable_classes(current)
        if classes:
            parts.append(current.tag + "".join(f".{c}" for c in classes))
        else:
            siblings = [c for c in (current.parent.children if current.parent else [])
                        if c.tag == current.tag]
            if len(siblings) > 1 and current.parent is not None:
                parts.append(f"{current.tag}:nth-of-type({siblings.index(current) + 1})")
            else:
                parts.append(current.tag)
        current = current.parent
    return " > ".join(reversed(parts)) or node.tag


def signature(node: Node) -> str:
    """Structural fingerprint used to spot repeated blocks (listing cards)."""
    attr = stable_attr(node)
    bits = [node.tag]
    if attr:
        bits.append(f"{attr[0]}={attr[1]}")
    classes = [c for c in node.classes() if class_quality(c) != "utility"]
    # Hashed classes are stable *within* one capture, so they still identify siblings.
    bits.extend(sorted(classes)[:4])
    if not classes and not attr:
        bits.append("kids:" + ",".join(sorted({c.tag for c in node.children})[:6]))
    return "|".join(bits)


def classify_field(name_hint: str, text: str) -> str:
    if PRICE_RE.search(text):
        return "price"
    if DATEISH_RE.search(text):
        return "date"
    low = name_hint.lower()
    for key in ("title", "name", "price", "location", "date", "image", "url", "link",
                "phone", "seller", "mileage", "year", "brand", "model", "id", "badge"):
        if key in low:
            return key
    if len(text) > 60:
        return "description"
    return "text"


def detect_repeated_blocks(dom: MiniDOM, base_url: str, max_groups: int = 6) -> list[dict]:
    """Find the containers whose children repeat -- i.e. the rows of a listing."""
    candidates = []
    for parent in dom.root.walk():
        if len(parent.children) < 3:
            continue
        groups: dict[str, list[Node]] = {}
        for child in parent.children:
            if child.tag in ("script", "style", "br", "template", "noscript"):
                continue
            groups.setdefault(signature(child), []).append(child)
        for sig, members in groups.items():
            if len(members) < 3:
                continue
            sample = members[0]
            links = sample.find_all("a", lambda n: bool(n.attrs.get("href")))
            text = sample.inner_text(600)
            if len(text) < 15 and not links:
                continue
            score = len(members) * (2 if links else 1) + min(len(text), 400) / 100
            candidates.append({"score": score, "parent": parent, "members": members,
                               "sig": sig, "sample": sample})
    candidates.sort(key=lambda c: -c["score"])

    seen_parents: set[int] = set()
    out = []
    for cand in candidates:
        if id(cand["parent"]) in seen_parents:
            continue
        seen_parents.add(id(cand["parent"]))
        sample: Node = cand["sample"]
        fields = []
        for link in sample.find_all("a", lambda n: bool(n.attrs.get("href")))[:3]:
            fields.append({
                "field": "url",
                "selector": css_for(link, sample) or "a",
                "attribute": "href",
                "sample": urllib.parse.urljoin(base_url, link.attrs.get("href", "")),
            })
        for img in sample.find_all("img")[:2]:
            src = img.attrs.get("src") or img.attrs.get("data-src") or img.attrs.get("srcset", "")
            fields.append({
                "field": "image",
                "selector": css_for(img, sample) or "img",
                "attribute": "src" if img.attrs.get("src") else "data-src",
                "sample": truncate(urllib.parse.urljoin(base_url, src.split()[0] if src else ""), 160),
            })
        for node in sample.walk():
            if node is sample or node.tag in ("script", "style", "svg", "path"):
                continue
            text = node.own_text()
            if not text or len(text) > 200:
                continue
            hint = " ".join([node.tag, node.attrs.get("class", ""),
                             (stable_attr(node) or ("", ""))[1]])
            fields.append({
                "field": classify_field(hint, text),
                "selector": css_for(node, sample),
                "attribute": "text",
                "sample": truncate(text, 120),
            })
            if len(fields) > 22:
                break
        out.append({
            "item_count": len(cand["members"]),
            "container_selector": css_for(cand["parent"]),
            "item_selector": css_for(sample, cand["parent"]),
            "item_signature": cand["sig"],
            "selector_durability": (
                "good (stable data-* / semantic hooks)"
                if stable_attr(sample) else
                "fragile (hashed or utility class names -- prefer JSON/API extraction)"
            ),
            "sample_text": truncate(sample.inner_text(400), 400),
            "fields": fields,
        })
        if len(out) >= max_groups:
            break
    return out


def extract_embedded_state(dom: MiniDOM) -> dict:
    """__NEXT_DATA__, JSON-LD, and friends: pre-rendered data, free of charge."""
    found: dict[str, object] = {}
    jsonld = []
    for script in dom.scripts:
        stype = (script.get("type") or "").lower()
        content = (script.get("content") or "").strip()
        if not content:
            continue
        if "ld+json" in stype:
            try:
                jsonld.append(json.loads(content))
            except Exception:
                pass
            continue
        sid = script.get("id") or ""
        if sid in ("__NEXT_DATA__", "__NUXT_DATA__") or "json" in stype:
            try:
                found[sid or f"script[type={stype}]"] = json.loads(content)
                continue
            except Exception:
                pass
        for name in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__",
                     "__APOLLO_STATE__", "__remixContext", "__data"):
            if name in content and name not in found:
                blob = extract_assignment(content, name)
                if blob is not None:
                    found[name] = blob
    if jsonld:
        found["application/ld+json"] = jsonld
    return found


def extract_assignment(script_text: str, name: str):
    """Pull `window.X = {...};` out of a script body by brace matching."""
    match = re.search(re.escape(name) + r"\s*=\s*", script_text)
    if not match:
        return None
    start = match.end()
    while start < len(script_text) and script_text[start] in " \t\r\n":
        start += 1
    if start >= len(script_text) or script_text[start] not in "{[":
        return None
    opening = script_text[start]
    closing = "}" if opening == "{" else "]"
    depth, in_string, quote, escaped = 0, False, "", False
    for idx in range(start, min(len(script_text), start + 4_000_000)):
        char = script_text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string, quote = True, char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                raw = script_text[start:idx + 1]
                try:
                    return json.loads(raw)
                except Exception:
                    return {"_unparsed_javascript_literal": truncate(raw, 2000)}
    return None


def find_pagination(dom: MiniDOM, base_url: str) -> dict:
    hints: dict[str, list] = {"rel_next": [], "numbered_links": [], "next_text_links": [],
                              "url_params": []}
    for link in dom.root.find_all("a"):
        href = link.attrs.get("href") or ""
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        rel = (link.attrs.get("rel") or "").lower()
        text = link.own_text() or link.inner_text(40)
        if "next" in rel:
            hints["rel_next"].append(absolute)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(absolute).query)
        for key in query:
            if PAGINATION_PARAM_RE.match(key):
                hints["url_params"].append(key)
                hints["numbered_links"].append(absolute)
                break
        if text.lower().strip() in ("next", "next page", ">", "›", "»", "التالي") or \
                re.match(r"^page\s*\d+$", text.lower().strip()):
            hints["next_text_links"].append(absolute)
    for link in dom.root.find_all("link", lambda n: "next" in (n.attrs.get("rel") or "").lower()):
        hints["rel_next"].append(urllib.parse.urljoin(base_url, link.attrs.get("href", "")))
    hints["url_params"] = sorted(set(hints["url_params"]))
    for key in ("rel_next", "numbered_links", "next_text_links"):
        hints[key] = sorted(set(hints[key]))[:10]
    return hints


# --------------------------------------------------------------------------------------
# static layer: stdlib only
# --------------------------------------------------------------------------------------

class RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.chain: list[dict] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append({"status": code, "from": req.full_url, "to": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, headers: dict | None = None, timeout: float = 30.0,
             method: str = "GET", body: bytes | None = None) -> dict:
    """One plain HTTP request. No dependencies, no browser, no magic."""
    redirects = RecordingRedirectHandler()
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cafile=os.environ.get("REQUESTS_CA_BUNDLE")
                                 or os.environ.get("SSL_CERT_FILE") or None)
    except Exception:
        ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(redirects, urllib.request.HTTPSHandler(context=ctx))
    request_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers, method=method, data=body)
    started = time.time()
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
            result = {
                "ok": True, "status": response.status, "final_url": response.url,
                "headers": dict(response.headers.items()),
                "set_cookie": response.headers.get_all("Set-Cookie") or [],
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        result = {
            "ok": False, "status": exc.code, "final_url": exc.url if hasattr(exc, "url") else url,
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "set_cookie": (exc.headers.get_all("Set-Cookie") or []) if exc.headers else [],
            "error": f"HTTP {exc.code} {exc.reason}",
        }
    except Exception as exc:
        return {"ok": False, "status": 0, "final_url": url, "headers": {}, "set_cookie": [],
                "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": int((time.time() - started) * 1000),
                "body": "", "bytes": 0, "redirects": redirects.chain}

    encoding = (result["headers"].get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif "br" in encoding:
            import brotli  # optional; only if the server insists on brotli
            raw = brotli.decompress(raw)
    except Exception:
        pass
    result["bytes"] = len(raw)
    result["body"] = raw.decode("utf-8", errors="replace")
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    result["redirects"] = redirects.chain
    return result


def probe_robots(url: str, quiet: bool = False) -> dict:
    parsed = urllib.parse.urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    info: dict = {"url": robots_url, "fetched": False, "allowed": None, "crawl_delay": None,
                  "sitemaps": [], "matching_rules": [], "body": ""}
    result = http_get(robots_url, timeout=15)
    if not result.get("body"):
        info["error"] = result.get("error", f"status {result.get('status')}")
        return info
    info["fetched"] = True
    info["status"] = result["status"]
    info["body"] = result["body"][:20000]
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(info["body"].splitlines())
    try:
        info["allowed"] = parser.can_fetch(DEFAULT_UA, url)
        info["allowed_wildcard_agent"] = parser.can_fetch("*", url)
        delay = parser.crawl_delay("*")
        info["crawl_delay"] = float(delay) if delay else None
    except Exception:
        pass
    info["sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", info["body"])
    path = parsed.path or "/"
    for line in info["body"].splitlines():
        if re.match(r"(?i)^\s*(dis)?allow:", line):
            rule = line.split(":", 1)[1].strip()
            if rule and (path.startswith(rule.rstrip("*")) or rule in ("/", "*")):
                info["matching_rules"].append(line.strip())
    info["matching_rules"] = info["matching_rules"][:15]
    return info


def probe_sitemaps(sitemap_urls: list[str], out_dir: Path, limit: int = 2) -> list[dict]:
    out = []
    for sm_url in sitemap_urls[:limit]:
        result = http_get(sm_url, timeout=25)
        body = result.get("body", "")
        entry = {"url": sm_url, "status": result.get("status"), "bytes": result.get("bytes", 0)}
        if body:
            name = "raw/" + slugify(urllib.parse.urlsplit(sm_url).path or "sitemap") + ".xml"
            write_text(out_dir / name, body)
            entry["saved_as"] = name
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            entry["is_index"] = "<sitemapindex" in body
            entry["loc_count"] = len(locs)
            entry["sample_locs"] = locs[:15]
        else:
            entry["error"] = result.get("error")
        out.append(entry)
    return out


# --------------------------------------------------------------------------------------
# browser layer: Playwright. Optional, but this is where the real capture happens.
# --------------------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(p)
  );
}
"""

COLLECT_GLOBALS_JS = """
(names) => {
  const seen = new WeakSet();
  const clone = (value, depth) => {
    if (value === null || typeof value !== 'object') {
      if (typeof value === 'string') return value.length > 2000 ? value.slice(0, 2000) + '...' : value;
      if (typeof value === 'function') return '[function]';
      return value;
    }
    if (depth > 6) return '[max depth]';
    if (seen.has(value)) return '[circular]';
    seen.add(value);
    if (Array.isArray(value)) {
      const head = value.slice(0, 50).map((v) => clone(v, depth + 1));
      if (value.length > 50) head.push('...+' + (value.length - 50) + ' items');
      return head;
    }
    const out = {};
    let n = 0;
    for (const key of Object.keys(value)) {
      if (n++ > 120) { out['...'] = 'truncated'; break; }
      try { out[key] = clone(value[key], depth + 1); } catch (e) { out[key] = '[throws]'; }
    }
    return out;
  };
  const wanted = new Set(names);
  for (const key of Object.getOwnPropertyNames(window)) {
    if (/^(__|_[A-Z])|(STATE|DATA|CONFIG|ENV|SETTINGS|CONTEXT)$/.test(key)) wanted.add(key);
  }
  const result = {};
  for (const key of wanted) {
    try {
      const value = window[key];
      if (value === undefined) continue;
      result[key] = clone(value, 0);
    } catch (e) { result[key] = '[inaccessible]'; }
  }
  return result;
}
"""

STORAGE_JS = """
() => {
  const dump = (store) => {
    const out = {};
    try {
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        const v = store.getItem(k) || '';
        out[k] = v.length > 4000 ? v.slice(0, 4000) + '...' : v;
      }
    } catch (e) { out['_error'] = String(e); }
    return out;
  };
  return {
    localStorage: dump(window.localStorage),
    sessionStorage: dump(window.sessionStorage),
    documentCookie: document.cookie,
    title: document.title,
    charset: document.characterSet,
    lang: document.documentElement.lang,
    metaGenerator: (document.querySelector('meta[name=generator]') || {}).content || '',
    scriptCount: document.scripts.length,
    domNodes: document.getElementsByTagName('*').length,
    jsonLdBlocks: document.querySelectorAll('script[type="application/ld+json"]').length,
    forms: Array.from(document.forms).slice(0, 12).map((f) => ({
      action: f.action, method: f.method, id: f.id, name: f.name,
      fields: Array.from(f.elements).slice(0, 25).map((e) => ({
        name: e.name, type: e.type, id: e.id, required: !!e.required,
        value: e.type === 'hidden' ? String(e.value || '').slice(0, 120) : undefined,
      })),
    })),
  };
}
"""


class BrowserCapture:
    """Owns the Playwright lifecycle for a run; one context (and HAR) per page."""

    def __init__(self, args):
        self.args = args
        self.playwright = None
        self.browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        launcher = getattr(self.playwright, self.args.browser)
        launch_kwargs = {
            "headless": not self.args.headed,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                     "--disable-dev-shm-usage"] if self.args.browser == "chromium" else [],
        }
        if self.args.proxy:
            launch_kwargs["proxy"] = {"server": self.args.proxy}
        try:
            self.browser = launcher.launch(**launch_kwargs)
        except Exception as exc:
            hint = ""
            if "Executable doesn't exist" in str(exc):
                hint = "\n  Run: playwright install " + self.args.browser
            raise RuntimeError(f"could not launch {self.args.browser}: {exc}{hint}") from exc
        return self

    def __exit__(self, *exc_info):
        for closer in (getattr(self.browser, "close", None),
                       getattr(self.playwright, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        return False

    def capture(self, url: str, page_dir: Path) -> dict:
        from playwright.sync_api import Error as PWError

        args = self.args
        width, height = args.viewport
        har_path = page_dir / "network" / "capture.har"
        har_path.parent.mkdir(parents=True, exist_ok=True)

        context_kwargs = {
            "user_agent": args.ua,
            "locale": args.locale,
            "timezone_id": args.timezone,
            "viewport": {"width": width, "height": height},
            "ignore_https_errors": True,
            "record_har_path": str(har_path),
            "record_har_content": "embed",
            "record_har_mode": "full",
            "service_workers": "allow",
        }
        if args.header:
            context_kwargs["extra_http_headers"] = dict(args.header)
        if args.storage_state:
            context_kwargs["storage_state"] = args.storage_state

        context = self.browser.new_context(**context_kwargs)
        context.set_default_timeout(args.timeout)
        if args.cookie:
            host = urllib.parse.urlsplit(url).hostname or ""
            context.add_cookies([
                {"name": k, "value": v, "domain": "." + registrable_domain(host), "path": "/"}
                for k, v in args.cookie
            ])
        if not args.no_stealth:
            context.add_init_script(STEALTH_JS)

        page = context.new_page()
        console: list[dict] = []
        ws_frames: list[dict] = []
        page_errors: list[str] = []

        page.on("console", lambda m: console.append(
            {"type": m.type, "text": truncate(m.text, 2000),
             "location": getattr(m, "location", None)}))
        page.on("pageerror", lambda e: page_errors.append(truncate(str(e), 1000)))

        def on_websocket(ws):
            ws_frames.append({"event": "open", "url": ws.url, "t": now_iso()})
            ws.on("framesent", lambda payload: ws_frames.append(
                {"event": "sent", "url": ws.url, "payload": truncate(str(payload), 2000)}))
            ws.on("framereceived", lambda payload: ws_frames.append(
                {"event": "received", "url": ws.url, "payload": truncate(str(payload), 2000)}))
            ws.on("close", lambda _=None: ws_frames.append({"event": "close", "url": ws.url}))

        page.on("websocket", on_websocket)

        if args.block_images:
            page.route(re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|woff2?|ttf|mp4)($|\?)"),
                       lambda route: route.abort())

        nav: dict = {"requested_url": url}
        started = time.time()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
            nav["status"] = response.status if response else None
            nav["status_text"] = response.status_text if response else None
            nav["response_headers"] = dict(response.headers) if response else {}
            nav["server_ip"] = None
            if response:
                try:
                    nav["server_ip"] = response.server_addr()
                except Exception:
                    pass
        except PWError as exc:
            nav["error"] = truncate(str(exc), 600)

        try:
            page.wait_for_load_state("networkidle", timeout=min(args.timeout, 20000))
        except PWError:
            pass

        if not args.no_consent:
            nav["consent_clicked"] = self._dismiss_consent(page)

        for selector in args.click or []:
            try:
                page.click(selector, timeout=5000)
                page.wait_for_timeout(1000)
            except PWError:
                pass

        nav["extra_wait_ms"] = args.wait
        page.wait_for_timeout(args.wait)

        interaction_started = None
        if args.scroll:
            interaction_started = datetime.now(timezone.utc)
            nav["scroll"] = self._scroll(page, args.scroll)

        nav["final_url"] = page.url
        nav["load_ms"] = int((time.time() - started) * 1000)

        rendered_html = ""
        try:
            rendered_html = page.content()
        except PWError as exc:
            nav["content_error"] = str(exc)
        write_text(page_dir / "raw" / "rendered.html", rendered_html)

        page_facts, globals_blob = {}, {}
        try:
            page_facts = page.evaluate(STORAGE_JS)
        except PWError:
            pass
        try:
            globals_blob = page.evaluate(COLLECT_GLOBALS_JS, list(INTERESTING_GLOBALS))
        except PWError:
            pass

        if not args.no_screenshots:
            for name, kwargs in (("viewport.png", {}), ("fullpage.png", {"full_page": True})):
                try:
                    page.screenshot(path=str(page_dir / "screens" / name), **kwargs)
                except PWError:
                    pass

        cookies = []
        try:
            cookies = context.cookies()
        except PWError:
            pass
        try:
            storage_path = page_dir / "storage" / "storage_state.json"
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_path))
        except (PWError, OSError):
            pass

        context.close()  # flushes the HAR

        if console:
            write_text(page_dir / "console" / "console.jsonl",
                       "\n".join(json.dumps(c, default=str) for c in console))
        if page_errors:
            write_json(page_dir / "console" / "page_errors.json", page_errors)
        if ws_frames:
            write_text(page_dir / "network" / "websockets.jsonl",
                       "\n".join(json.dumps(f, default=str) for f in ws_frames))

        return {
            "navigation": nav,
            "rendered_html": rendered_html,
            "page_facts": page_facts,
            "globals": globals_blob,
            "cookies": cookies,
            "console_count": len(console),
            "console_errors": [c for c in console if c["type"] == "error"][:20],
            "page_errors": page_errors[:20],
            "websocket_frames": len(ws_frames),
            "har_path": har_path,
            "interaction_started": interaction_started,
        }

    @staticmethod
    def _dismiss_consent(page) -> str | None:
        from playwright.sync_api import Error as PWError

        for selector in CONSENT_SELECTORS:
            try:
                element = page.locator(selector).first
                if element.count() and element.is_visible(timeout=1000):
                    element.click(timeout=3000)
                    page.wait_for_timeout(800)
                    return selector
            except PWError:
                continue
            except Exception:
                continue
        for text in CONSENT_TEXTS:
            try:
                button = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
                if button.count() and button.is_visible(timeout=800):
                    button.click(timeout=3000)
                    page.wait_for_timeout(800)
                    return f'role=button[name="{text}"]'
            except Exception:
                continue
        return None

    @staticmethod
    def _scroll(page, steps: int) -> dict:
        from playwright.sync_api import Error as PWError

        heights = []
        for _ in range(steps):
            try:
                heights.append(page.evaluate("document.body.scrollHeight"))
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
                page.wait_for_timeout(1200)
            except PWError:
                break
        try:
            heights.append(page.evaluate("document.body.scrollHeight"))
        except PWError:
            pass
        grew = len(heights) > 1 and heights[-1] > heights[0] * 1.2
        return {"steps": steps, "scroll_heights": heights,
                "page_grew": grew,
                "verdict": "infinite scroll / lazy loading likely" if grew
                           else "no significant growth from scrolling"}


# --------------------------------------------------------------------------------------
# HAR -> network records -> API analysis
# --------------------------------------------------------------------------------------

def parse_har(har_path: Path, page_dir: Path, args, interaction_started) -> list[dict]:
    """Turn the HAR into flat records, saving interesting response bodies to disk."""
    if not har_path.exists():
        return []
    try:
        har = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []

    records: list[dict] = []
    bodies_dir = page_dir / "bodies"
    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        request = entry.get("request", {}) or {}
        response = entry.get("response", {}) or {}
        content = response.get("content", {}) or {}
        url = request.get("url", "")
        parsed = urllib.parse.urlsplit(url)
        mime = content.get("mimeType") or ""
        req_headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in request.get("headers", [])}
        res_headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in response.get("headers", [])}
        post = request.get("postData") or {}
        started_at = entry.get("startedDateTime", "")

        record = {
            "index": index,
            "method": request.get("method", ""),
            "url": url,
            "host": parsed.netloc,
            "path": parsed.path,
            "path_template": normalise_path(url),
            "query": {k: v[:2] for k, v in urllib.parse.parse_qs(parsed.query).items()},
            "resource_type": entry.get("_resourceType", ""),
            "status": response.get("status", 0),
            "mime": mime,
            "size": content.get("size", -1),
            "time_ms": round(entry.get("time", 0), 1),
            "started": started_at,
            "http_version": response.get("httpVersion", ""),
            "server_ip": entry.get("serverIPAddress", ""),
            "from_cache": bool(entry.get("_fromCache") or response.get("_transferSize") == 0),
            "request_headers": {k: redact_value(k, v, args.redact) for k, v in req_headers.items()},
            "response_headers": res_headers,
            "set_cookie": [h.get("value", "") for h in response.get("headers", [])
                           if h.get("name", "").lower() == "set-cookie"],
            "post_mime": post.get("mimeType", ""),
            "post_body": truncate(post.get("text", ""), 8000) if post.get("text") else "",
            "redirect_to": response.get("redirectURL", ""),
            "third_party": bool(TRACKER_HOSTS.search(parsed.netloc)),
            "after_interaction": bool(
                interaction_started and started_at
                and started_at >= interaction_started.strftime("%Y-%m-%dT%H:%M:%S")
            ),
            "body_file": None,
            "body_json": None,
        }

        text = content.get("text")
        if text and (is_texty(mime) or args.save_all_bodies):
            raw = text
            if content.get("encoding") == "base64":
                try:
                    decoded = base64.b64decode(text)
                    raw = decoded.decode("utf-8", errors="replace") if is_texty(mime) else ""
                except Exception:
                    raw = ""
            if raw and len(raw.encode("utf-8", "ignore")) <= args.max_body_kb * 1024:
                ext = (".json" if "json" in mime else ".js" if "javascript" in mime
                       else ".html" if "html" in mime else ".css" if "css" in mime
                       else ".xml" if "xml" in mime else ".txt")
                name = slugify(f"{index:03d}-{parsed.netloc}-{Path(parsed.path).name or 'index'}", 70)
                rel = f"bodies/{name}{ext}"
                write_text(bodies_dir / f"{name}{ext}", raw)
                record["body_file"] = rel
                if "json" in mime or raw.lstrip()[:1] in "{[":
                    try:
                        record["body_json"] = json.loads(raw)
                    except Exception:
                        pass
        records.append(record)

    write_text(page_dir / "network" / "network.jsonl", "\n".join(
        json.dumps({k: v for k, v in r.items() if k != "body_json"}, default=str)
        for r in records))
    if not args.keep_har:
        try:
            har_path.unlink()
        except OSError:
            pass
    return records


def api_candidates(records: list[dict], first_party: str) -> list[dict]:
    """Group the data-bearing calls into endpoint templates a scraper can drive."""
    groups: dict[tuple, dict] = {}
    for record in records:
        if record["third_party"] or record["status"] in (0, 204, 301, 302, 304):
            continue
        mime, rtype = record["mime"].lower(), record["resource_type"]
        is_data = "json" in mime or "graphql" in mime or "xml" in mime and rtype in ("xhr", "fetch")
        if rtype not in ("xhr", "fetch", "websocket", "eventsource") and not is_data:
            continue
        if not is_data and record["body_json"] is None:
            continue
        key = (record["method"], record["host"], record["path_template"])
        group = groups.setdefault(key, {
            "method": record["method"],
            "host": record["host"],
            "path_template": record["path_template"],
            "first_party": registrable_domain(record["host"]) == first_party,
            "calls": 0,
            "statuses": set(),
            "mimes": set(),
            "query_params": {},
            "auth_headers": {},
            "post_bodies": [],
            "sample_url": record["url"],
            "sample_body_file": None,
            "response_shape": None,
            "record_arrays": [],
            "triggered_by_scroll": False,
            "total_bytes": 0,
            "sample_request_headers": {},
        })
        group["calls"] += 1
        group["statuses"].add(record["status"])
        group["mimes"].add(record["mime"].split(";")[0])
        group["total_bytes"] += max(record["size"], 0)
        group["triggered_by_scroll"] |= record["after_interaction"]
        for name, values in record["query"].items():
            group["query_params"].setdefault(name, set()).add(str(values[0])[:60])
        for name, value in record["request_headers"].items():
            if AUTHISH_HEADERS.match(name):
                group["auth_headers"][name] = truncate(value, 200)
        if record["post_body"] and len(group["post_bodies"]) < 2:
            group["post_bodies"].append(record["post_body"])
        if not group["sample_request_headers"]:
            group["sample_request_headers"] = record["request_headers"]
        if record["body_json"] is not None and group["response_shape"] is None:
            group["response_shape"] = infer_shape(record["body_json"])
            group["record_arrays"] = find_record_arrays(record["body_json"])
            group["sample_body_file"] = record["body_file"]
        elif group["sample_body_file"] is None:
            group["sample_body_file"] = record["body_file"]

    out = []
    for group in groups.values():
        group["statuses"] = sorted(group["statuses"])
        group["mimes"] = sorted(m for m in group["mimes"] if m)
        group["query_params"] = {k: sorted(v)[:3] for k, v in group["query_params"].items()}
        group["pagination_params"] = [k for k in group["query_params"]
                                      if PAGINATION_PARAM_RE.match(k)]
        group["record_count"] = sum(a["count"] for a in group["record_arrays"])
        out.append(group)
    out.sort(key=lambda g: (not g["first_party"], -g["record_count"], -g["total_bytes"]))
    return out


def graphql_operations(records: list[dict]) -> list[dict]:
    ops = []
    for record in records:
        if not record["post_body"] or "graphql" not in (record["url"] + record["post_mime"]).lower():
            if "graphql" not in record["url"].lower():
                continue
        payload = None
        try:
            payload = json.loads(record["post_body"]) if record["post_body"] else None
        except Exception:
            pass
        entries = payload if isinstance(payload, list) else [payload] if payload else []
        for item in entries:
            if not isinstance(item, dict):
                continue
            ops.append({
                "endpoint": record["url"].split("?")[0],
                "method": record["method"],
                "operationName": item.get("operationName"),
                "variables": truncate(json.dumps(item.get("variables", {}), default=str), 1200),
                "query": truncate(item.get("query", ""), 2500),
                "persisted_hash": (item.get("extensions") or {}).get("persistedQuery", {}).get("sha256Hash")
                if isinstance(item.get("extensions"), dict) else None,
                "status": record["status"],
                "response_file": record["body_file"],
            })
        if not entries and record["query"]:
            ops.append({"endpoint": record["url"].split("?")[0], "method": record["method"],
                        "query_string_params": record["query"], "status": record["status"],
                        "response_file": record["body_file"]})
    return ops[:25]


def replay_endpoints(candidates: list[dict], args, quiet=False) -> list[dict]:
    """The question that decides the scraper's architecture: does it work without a browser?"""
    results = []
    tried = 0
    for group in candidates:
        if tried >= args.replay_limit:
            break
        if not group["first_party"] or group["method"] not in ("GET", "POST"):
            continue
        if group["method"] == "POST" and not args.replay_post:
            continue
        if group["record_count"] == 0 and "json" not in " ".join(group["mimes"]):
            continue
        url = group["sample_url"]
        tried += 1
        log(f"replay {group['method']} {truncate(url, 110)}", quiet, "..")
        body = None
        headers_full = {k: v for k, v in group["sample_request_headers"].items()
                        if k not in ("host", "content-length", "connection", ":authority",
                                     ":method", ":path", ":scheme", "accept-encoding")}
        if group["method"] == "POST" and group["post_bodies"]:
            body = group["post_bodies"][0].encode("utf-8")
        attempts = {}
        for label, headers in (("with_captured_headers", headers_full),
                               ("plain_ua_only", {"User-Agent": args.ua, "Accept": "*/*"})):
            time.sleep(args.delay)
            result = http_get(url, headers=headers, timeout=25, method=group["method"], body=body)
            text = result.get("body", "")
            parsed_json, keys = None, None
            try:
                parsed_json = json.loads(text)
                keys = sorted(parsed_json.keys())[:20] if isinstance(parsed_json, dict) else "list"
            except Exception:
                pass
            attempts[label] = {
                "status": result.get("status"),
                "error": result.get("error"),
                "bytes": result.get("bytes", 0),
                "content_type": result.get("headers", {}).get("Content-Type", ""),
                "json_parsed": parsed_json is not None,
                "top_level_keys": keys,
                "record_arrays": [{"path": a["path"], "count": a["count"]}
                                  for a in find_record_arrays(parsed_json)] if parsed_json else [],
                "body_head": truncate(text, 400),
            }
        verdict = "blocked"
        if attempts["plain_ua_only"]["json_parsed"] and attempts["plain_ua_only"]["record_arrays"]:
            verdict = "works with NO auth (best case: plain HTTP GET)"
        elif attempts["plain_ua_only"]["json_parsed"]:
            verdict = "works with no auth, but payload differs / has no record array"
        elif attempts["with_captured_headers"]["json_parsed"]:
            verdict = "works ONLY with captured headers/cookies (bootstrap a session first)"
        elif attempts["with_captured_headers"]["status"] in (401, 403):
            verdict = f"rejected ({attempts['with_captured_headers']['status']}) -- token likely bound to browser/TLS fingerprint"
        results.append({
            "method": group["method"],
            "url": url,
            "path_template": group["path_template"],
            "verdict": verdict,
            "required_headers": sorted(group["auth_headers"].keys()),
            "attempts": attempts,
        })
    return results


# --------------------------------------------------------------------------------------
# fingerprinting
# --------------------------------------------------------------------------------------

def build_evidence(page: dict) -> dict:
    static = page.get("static") or {}
    browser = page.get("browser") or {}
    records = page.get("network") or []
    headers = flatten_headers(static.get("headers", {}))
    headers += flatten_headers((browser.get("navigation") or {}).get("response_headers", {}))
    for record in records[:400]:
        headers += flatten_headers(record.get("response_headers", {}))
    cookies = [c.get("name", "") for c in browser.get("cookies", [])]
    cookies += [re.split(r"[=;]", c)[0].strip() for c in static.get("set_cookie", [])]
    for record in records:
        cookies += [re.split(r"[=;]", c)[0].strip() for c in record.get("set_cookie", [])]
    urls = [r["url"] for r in records] or []
    urls += re.findall(r'src=["\']([^"\']+)', static.get("body", "")[:400000])
    html = (browser.get("rendered_html") or "")[:600000] + static.get("body", "")[:400000]
    globals_seen = list((browser.get("globals") or {}).keys())
    return {"header": headers, "cookie": [c for c in cookies if c], "url": urls,
            "html": [html], "global": globals_seen}


def match_signs(evidence: dict, signs, with_severity: bool = False) -> list[dict]:
    found = []
    for sign in signs:
        if with_severity:
            name, severity, patterns = sign
        else:
            name, patterns = sign
            severity = None
        hits = []
        for source, pattern in patterns:
            regex = re.compile(pattern, re.I)
            for item in evidence.get(source, []):
                match = regex.search(item or "")
                if match:
                    hits.append({"where": source, "evidence": truncate(
                        item if source != "html" else match.group(0), 160)})
                    break
            if len(hits) >= 3:
                break
        if hits:
            entry = {"name": name, "evidence": hits}
            if severity:
                entry["scraping_impact"] = severity
            found.append(entry)
    return found


def difficulty_verdict(pages: list[dict], antibot: list[dict], replays: list[dict]) -> dict:
    reasons, strategy = [], None
    high = [a["name"] for a in antibot if a.get("scraping_impact") == "high"]
    replay_free = [r for r in replays if r["verdict"].startswith("works with NO auth")]
    replay_headers = [r for r in replays if r["verdict"].startswith("works ONLY")]

    js_required = False
    for page in pages:
        static_blocks = len((page.get("static_analysis") or {}).get("repeated_blocks") or [])
        rendered_blocks = len((page.get("dom_analysis") or {}).get("repeated_blocks") or [])
        static_items = max((b["item_count"] for b in
                            ((page.get("static_analysis") or {}).get("repeated_blocks") or [])),
                           default=0)
        rendered_items = max((b["item_count"] for b in
                              ((page.get("dom_analysis") or {}).get("repeated_blocks") or [])),
                             default=0)
        if rendered_items > max(static_items * 2, static_items + 5) or (
                rendered_blocks and not static_blocks):
            js_required = True
            reasons.append(
                f"{page['url']}: rendered DOM exposes {rendered_items} repeated items vs "
                f"{static_items} in the server HTML -- content is client-rendered")
        elif static_items:
            reasons.append(
                f"{page['url']}: the server HTML already contains {static_items} repeated items "
                f"-- no browser needed for extraction")
        if (page.get("embedded_state_static") or {}):
            reasons.append(
                f"{page['url']}: server HTML embeds state blobs "
                f"({', '.join(list(page['embedded_state_static'])[:4])}) -- parse those, not the DOM")

    if replay_free:
        strategy = "A. Call the site's own JSON API directly with an HTTP client (httpx/requests)."
    elif replay_headers:
        strategy = ("B. Call the JSON API with a bootstrapped session: load one page in a browser "
                    "(or replicate the token-minting call), harvest the required headers/cookies, "
                    "then issue plain HTTP requests until they expire.")
    elif any((p.get("embedded_state_static") or {}) for p in pages):
        strategy = ("C. Fetch the HTML with an HTTP client and parse the embedded JSON state blob "
                    "(__NEXT_DATA__ / __NUXT__ / JSON-LD). No DOM selectors, no browser.")
    elif not js_required:
        strategy = "D. Fetch the HTML with an HTTP client and parse it with CSS selectors."
    else:
        strategy = ("E. Drive a real browser (Playwright) and read the rendered DOM, or intercept "
                    "the in-page XHR responses. Slowest path -- use it only if A-D fail.")

    if high:
        reasons.append(f"anti-bot with real teeth in the path: {', '.join(high)}")
    score = 1 + (2 if high else 0) + (1 if js_required else 0) + (0 if (replay_free or replay_headers) else 1)
    return {
        "difficulty": ["trivial", "easy", "moderate", "hard", "very hard"][min(score, 4)],
        "recommended_strategy": strategy,
        "javascript_required_for_content": js_required,
        "blocking_vendors": high,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------------------
# per-page orchestration
# --------------------------------------------------------------------------------------

def probe_page(url: str, out_dir: Path, args, browser: "BrowserCapture | None") -> dict:
    page_dir = out_dir / "pages" / slugify(
        (urllib.parse.urlsplit(url).netloc + urllib.parse.urlsplit(url).path) or "root", 70)
    page_dir.mkdir(parents=True, exist_ok=True)
    page: dict = {"url": url, "dir": str(page_dir.relative_to(out_dir))}

    log(f"static fetch {url}", args.quiet, "01")
    static = http_get(url, headers=dict(args.header or []), timeout=args.timeout / 1000)
    write_text(page_dir / "raw" / "initial.html", static.get("body", ""))
    page["static"] = static
    page["static_summary"] = {
        "status": static.get("status"), "final_url": static.get("final_url"),
        "bytes": static.get("bytes"), "elapsed_ms": static.get("elapsed_ms"),
        "redirects": static.get("redirects"), "error": static.get("error"),
        "server": static.get("headers", {}).get("Server"),
        "content_type": static.get("headers", {}).get("Content-Type"),
        "set_cookie_names": [re.split(r"[=;]", c)[0].strip() for c in static.get("set_cookie", [])],
    }

    if static.get("body"):
        dom = MiniDOM(static["body"])
        page["embedded_state_static"] = extract_embedded_state(dom)
        page["static_analysis"] = {
            "title": next((n.inner_text(200) for n in dom.root.find_all("title")), ""),
            "repeated_blocks": detect_repeated_blocks(dom, url),
            "pagination": find_pagination(dom, url),
            "script_count": len(dom.scripts),
            "external_scripts": [s["src"] for s in dom.scripts if s["src"]][:40],
            "meta": {
                (n.attrs.get("name") or n.attrs.get("property") or ""): truncate(
                    n.attrs.get("content", ""), 200)
                for n in dom.root.find_all("meta")
                if (n.attrs.get("name") or n.attrs.get("property"))
            },
        }
        for name, blob in (page.get("embedded_state_static") or {}).items():
            write_json(page_dir / "state" / f"static-{slugify(name, 40)}.json", blob)

    if browser is not None:
        log(f"browser capture {url}", args.quiet, "02")
        capture = browser.capture(url, page_dir)
        records = parse_har(capture.pop("har_path"), page_dir, args,
                            capture.pop("interaction_started"))
        page["network"] = records
        page["browser"] = capture
        rendered = capture.get("rendered_html") or ""
        if rendered:
            dom = MiniDOM(rendered)
            page["embedded_state_rendered"] = extract_embedded_state(dom)
            page["dom_analysis"] = {
                "repeated_blocks": detect_repeated_blocks(dom, capture["navigation"].get(
                    "final_url", url)),
                "pagination": find_pagination(dom, capture["navigation"].get("final_url", url)),
            }
            for name, blob in (page.get("embedded_state_rendered") or {}).items():
                write_json(page_dir / "state" / f"rendered-{slugify(name, 40)}.json", blob)
        if capture.get("globals"):
            write_json(page_dir / "state" / "window_globals.json", capture["globals"])
        if capture.get("cookies"):
            write_json(page_dir / "storage" / "cookies.json", [
                {**c, "value": redact_value("cookie", c.get("value", ""), args.redact)}
                for c in capture["cookies"]])
        if capture.get("page_facts"):
            write_json(page_dir / "state" / "page_facts.json", capture["page_facts"])
        page["network_summary"] = summarise_network(records)

    return page


def summarise_network(records: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    by_host: dict[str, int] = {}
    statuses: dict[str, int] = {}
    total_bytes = 0
    for record in records:
        by_type[record["resource_type"] or "other"] = by_type.get(record["resource_type"] or "other", 0) + 1
        by_host[record["host"]] = by_host.get(record["host"], 0) + 1
        statuses[str(record["status"])] = statuses.get(str(record["status"]), 0) + 1
        total_bytes += max(record["size"], 0)
    return {
        "requests": len(records),
        "total_response_bytes": total_bytes,
        "by_resource_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "top_hosts": dict(sorted(by_host.items(), key=lambda kv: -kv[1])[:25]),
        "statuses": dict(sorted(statuses.items())),
    }


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------

def md_json(obj, limit: int = 3000) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated -- see report.json)"
    return f"```json\n{text}\n```"


def endpoint_curl(group: dict, redact: bool) -> str:
    parts = [f"curl -sS -X {group['method']} '{group['sample_url']}'"]
    for name, value in list(group.get("auth_headers", {}).items())[:8]:
        shown = value if not redact else redact_value(name, value, True)
        parts.append(f"  -H '{name}: {shown}'")
    if group.get("post_bodies"):
        body = group["post_bodies"][0].replace("'", "'\\''")
        parts.append(f"  --data '{truncate(body, 600)}'")
    return " \\\n".join(parts)


def render_report_md(report: dict) -> str:
    out: list[str] = []
    add = out.append
    site = report["site"]
    add(f"# Site study: {site['registrable_domain']}")
    add("")
    add(f"- captured: `{report['captured_at']}` by webprobe {report['webprobe_version']}")
    add(f"- pages: {', '.join('`' + p['url'] + '`' for p in report['pages'])}")
    add(f"- mode: {'browser + static' if report['config']['browser_used'] else 'static only'}")
    add("")

    verdict = report["assessment"]
    add("## Verdict")
    add("")
    add(f"**Difficulty: {verdict['difficulty']}.** {verdict['recommended_strategy']}")
    add("")
    if verdict["blocking_vendors"]:
        add(f"- anti-bot in the path: **{', '.join(verdict['blocking_vendors'])}**")
    add(f"- JavaScript required to see the content: **{verdict['javascript_required_for_content']}**")
    for reason in verdict["reasons"]:
        add(f"- {reason}")
    add("")

    add("## Stack and infrastructure")
    add("")
    if report["stack"]:
        for item in report["stack"]:
            evidence = "; ".join(f"{e['where']}: `{e['evidence']}`" for e in item["evidence"][:2])
            add(f"- **{item['name']}** — {evidence}")
    else:
        add("- nothing recognised (unusual; check `report.json` -> pages[].static.headers)")
    add("")

    add("## Protection / anti-bot")
    add("")
    if report["antibot"]:
        for item in report["antibot"]:
            evidence = "; ".join(f"{e['where']}: `{e['evidence']}`" for e in item["evidence"][:2])
            add(f"- **{item['name']}** (impact: {item['scraping_impact']}) — {evidence}")
    else:
        add("- no known vendor fingerprints found.")
    add("")

    add("## First-party API endpoints")
    add("")
    first_party = [g for g in report["api_endpoints"] if g["first_party"]]
    if not first_party:
        add("None observed. The page's data arrives with the HTML (server-rendered) or through "
            "endpoints this capture did not trigger — try `--scroll 8`, or interact with filters "
            "and pagination while re-running with `--headed`.")
    for group in first_party[:20]:
        add(f"### `{group['method']} {group['host']}{group['path_template']}`")
        add("")
        add(f"- calls: {group['calls']} · statuses: {group['statuses']} · "
            f"types: {', '.join(group['mimes']) or 'n/a'} · "
            f"{group['total_bytes']:,} bytes")
        if group["record_arrays"]:
            for array in group["record_arrays"][:3]:
                add(f"- **records: `{array['path']}` holds {array['count']} items** with keys: "
                    f"`{', '.join(array['shared_keys'][:18])}`")
        if group["query_params"]:
            add(f"- query params: " + ", ".join(
                f"`{k}={v[0] if v else ''}`" for k, v in list(group["query_params"].items())[:14]))
        if group["pagination_params"]:
            add(f"- **pagination params: `{', '.join(group['pagination_params'])}`**")
        if group["triggered_by_scroll"]:
            add("- fired only after scrolling → this is the infinite-scroll feed")
        if group["auth_headers"]:
            add(f"- auth-ish request headers: `{', '.join(group['auth_headers'].keys())}`")
        if group["post_bodies"]:
            add("- request body:")
            add(f"```\n{truncate(group['post_bodies'][0], 1200)}\n```")
        if group["sample_body_file"]:
            add(f"- sample response saved at `{group['sample_body_file']}`")
        add("")
        add("<details><summary>reproduce</summary>")
        add("")
        add(f"```bash\n{endpoint_curl(group, report['config']['redact'])}\n```")
        add("")
        if group["response_shape"]:
            add("response shape:")
            add("")
            add(md_json(group["response_shape"], 2500))
        add("")
        add("</details>")
        add("")

    third_party = [g for g in report["api_endpoints"] if not g["first_party"]]
    if third_party:
        add(f"<details><summary>{len(third_party)} third-party data endpoints "
            f"(search vendors, CDNs, analytics)</summary>")
        add("")
        for group in third_party[:25]:
            add(f"- `{group['method']} {group['host']}{group['path_template']}` "
                f"({group['calls']}x, {group['record_count']} records)")
        add("")
        add("</details>")
        add("")

    if report["replay"]:
        add("## Can we skip the browser? (endpoint replay from plain Python)")
        add("")
        add("| endpoint | verdict | with headers | UA only |")
        add("|---|---|---|---|")
        for item in report["replay"]:
            with_h = item["attempts"]["with_captured_headers"]
            plain = item["attempts"]["plain_ua_only"]
            add(f"| `{item['method']} {truncate(item['path_template'], 60)}` | {item['verdict']} | "
                f"{with_h['status']} ({with_h['bytes']:,}B) | {plain['status']} ({plain['bytes']:,}B) |")
        add("")

    if report["graphql"]:
        add("## GraphQL operations")
        add("")
        for op in report["graphql"][:10]:
            add(f"- `{op.get('operationName') or '(anonymous)'}` → {op['endpoint']} "
                f"(status {op['status']})")
            if op.get("persisted_hash"):
                add(f"  - persisted query hash: `{op['persisted_hash']}`")
            if op.get("query"):
                add(f"```graphql\n{truncate(op['query'], 1200)}\n```")
        add("")

    add("## Embedded state in the HTML")
    add("")
    any_state = False
    for page in report["pages"]:
        for label, key in (("server HTML", "embedded_state_static"),
                           ("rendered DOM", "embedded_state_rendered")):
            blobs = page.get(key) or {}
            if blobs:
                any_state = True
                add(f"- `{page['url']}` ({label}): " + ", ".join(
                    f"**{name}** ({describe_blob(blob)})" for name, blob in blobs.items()))
    if not any_state:
        add("- none found.")
    add("")

    add("## DOM extraction plan")
    add("")
    for page in report["pages"]:
        blocks = ((page.get("dom_analysis") or {}).get("repeated_blocks")
                  or (page.get("static_analysis") or {}).get("repeated_blocks") or [])
        add(f"### `{page['url']}`")
        add("")
        if not blocks:
            add("No repeated block structure detected.")
            add("")
            continue
        for block in blocks[:3]:
            add(f"- **{block['item_count']} repeated items**")
            add(f"  - container: `{block['container_selector']}`")
            add(f"  - item: `{block['item_selector']}`")
            add(f"  - selector durability: {block['selector_durability']}")
            seen = set()
            for field in block["fields"]:
                key = (field["field"], field["selector"])
                if key in seen:
                    continue
                seen.add(key)
                add(f"  - `{field['field']}` ← `{field['selector']}` [{field['attribute']}] "
                    f"e.g. {field['sample']!r}")
            add("")

    add("## Pagination")
    add("")
    for page in report["pages"]:
        pagination = ((page.get("dom_analysis") or {}).get("pagination")
                      or (page.get("static_analysis") or {}).get("pagination") or {})
        bits = []
        if pagination.get("rel_next"):
            bits.append(f"`rel=next` → {pagination['rel_next'][0]}")
        if pagination.get("url_params"):
            bits.append(f"URL params `{', '.join(pagination['url_params'])}`")
        if pagination.get("numbered_links"):
            bits.append(f"e.g. {pagination['numbered_links'][0]}")
        add(f"- `{page['url']}`: " + ("; ".join(bits) if bits else "no link-based pagination found"))
    api_pagination = sorted({p for g in report["api_endpoints"] if g["first_party"]
                             for p in g["pagination_params"]})
    if api_pagination:
        add(f"- API pagination params observed: `{', '.join(api_pagination)}`")
    scroll_notes = [f"`{p['url']}`: {(p.get('browser') or {}).get('navigation', {}).get('scroll', {}).get('verdict')}"
                    for p in report["pages"]
                    if (p.get("browser") or {}).get("navigation", {}).get("scroll")]
    for note in scroll_notes:
        add(f"- {note}")
    add("")

    add("## robots.txt and sitemaps")
    add("")
    robots = report["robots"]
    add(f"- `{robots.get('url')}` fetched: {robots.get('fetched')} "
        f"(allowed for our UA: {robots.get('allowed')})")
    if robots.get("crawl_delay"):
        add(f"- **Crawl-delay: {robots['crawl_delay']}s** — use it.")
    for rule in robots.get("matching_rules", [])[:10]:
        add(f"  - `{rule}`")
    for sitemap in report.get("sitemaps", []):
        add(f"- sitemap `{sitemap['url']}`: status {sitemap.get('status')}, "
            f"{sitemap.get('loc_count', 0)} locs"
            + (" (index)" if sitemap.get("is_index") else ""))
        for loc in sitemap.get("sample_locs", [])[:3]:
            add(f"  - {loc}")
    add("")

    add("## Network shape")
    add("")
    for page in report["pages"]:
        summary = page.get("network_summary")
        if not summary:
            continue
        add(f"- `{page['url']}`: {summary['requests']} requests, "
            f"{summary['total_response_bytes']:,} bytes, "
            f"types {summary['by_resource_type']}")
        add(f"  - top hosts: {', '.join(list(summary['top_hosts'])[:8])}")
    add("")

    add("## Artifacts")
    add("")
    add("```")
    add(report["artifact_tree"])
    add("```")
    add("")
    add("---")
    add("")
    add("Captured request headers and cookies in this directory are session credentials. "
        "Re-run with `--redact` before sharing.")
    return "\n".join(out)


def describe_blob(blob) -> str:
    if isinstance(blob, dict):
        return f"object, keys: {', '.join(list(blob.keys())[:8])}"
    if isinstance(blob, list):
        return f"array of {len(blob)}"
    return type(blob).__name__


def render_hermes_brief(report: dict, args) -> str:
    site = report["site"]
    verdict = report["assessment"]
    first_party = [g for g in report["api_endpoints"] if g["first_party"]]
    replay_ok = [r for r in report["replay"] if r["verdict"].startswith("works")]
    out: list[str] = []
    add = out.append

    add(f"# Build a scraper for {site['registrable_domain']}")
    add("")
    add("## Goal")
    add("")
    add(args.goal or (
        f"Extract the structured records listed on {site['start_url']} into JSONL, "
        "with pagination, retries and resumability."))
    if args.target_fields:
        add("")
        add("Required fields per record: " + ", ".join(f"`{f}`" for f in args.target_fields) + ".")
    add("")
    add("## What has already been established")
    add("")
    add("A capture tool (`webprobe`) loaded the page(s) in a real browser and recorded the entire "
        "request/response surface. The findings below are observed facts from that capture, not "
        "guesses. The accompanying directory holds the raw evidence: `report.json` (everything), "
        "`REPORT.md` (readable), `pages/*/raw/` (server HTML + rendered DOM), "
        "`pages/*/network/network.jsonl` (every request), `pages/*/bodies/` (response bodies), "
        "`pages/*/state/` (embedded JSON state).")
    add("")
    add(f"- **Site stack**: {', '.join(s['name'] for s in report['stack']) or 'unidentified'}")
    add(f"- **Anti-bot**: {', '.join(a['name'] + ' (' + a['scraping_impact'] + ')' for a in report['antibot']) or 'none detected'}")
    add(f"- **JavaScript required to see content**: {verdict['javascript_required_for_content']}")
    add(f"- **Assessed difficulty**: {verdict['difficulty']}")
    add("")
    add("## Required approach")
    add("")
    add(verdict["recommended_strategy"])
    add("")
    for reason in verdict["reasons"][:8]:
        add(f"- {reason}")
    add("")

    if first_party:
        add("## The endpoints to drive")
        add("")
        for group in first_party[:8]:
            add(f"### `{group['method']} https://{group['host']}{group['path_template']}`")
            add("")
            add(f"- observed {group['calls']}x, statuses {group['statuses']}")
            if group["record_arrays"]:
                array = group["record_arrays"][0]
                add(f"- records live at `{array['path']}` ({array['count']} per response)")
                add(f"- per-record keys: `{', '.join(array['shared_keys'][:25])}`")
                add("- one real record:")
                add(md_json(array["sample"], 1800))
            if group["query_params"]:
                add("- query parameters seen: " + ", ".join(
                    f"`{k}={(v[0] if v else '')}`" for k, v in list(group["query_params"].items())[:16]))
            if group["pagination_params"]:
                add(f"- paginate with `{', '.join(group['pagination_params'])}`")
            if group["auth_headers"]:
                add(f"- these request headers were present and may be required: "
                    f"`{', '.join(group['auth_headers'].keys())}`")
            if group["post_bodies"]:
                add("- request body template:")
                add(f"```\n{truncate(group['post_bodies'][0], 1000)}\n```")
            add("")
            add(f"```bash\n{endpoint_curl(group, report['config']['redact'])}\n```")
            add("")
        if replay_ok:
            add("Replay results from plain Python (no browser):")
            add("")
            for item in replay_ok[:8]:
                add(f"- `{item['method']} {item['path_template']}` → {item['verdict']}")
            add("")

    state_pages = [(p["url"], p.get("embedded_state_static") or p.get("embedded_state_rendered") or {})
                   for p in report["pages"]]
    state_pages = [(u, s) for u, s in state_pages if s]
    if state_pages:
        add("## Embedded state (parse this instead of the DOM where possible)")
        add("")
        for url, blobs in state_pages:
            for name, blob in list(blobs.items())[:4]:
                add(f"- `{url}` → `{name}`: {describe_blob(blob)}")
        add("")

    add("## DOM fallback: selectors observed")
    add("")
    for page in report["pages"]:
        blocks = ((page.get("dom_analysis") or {}).get("repeated_blocks")
                  or (page.get("static_analysis") or {}).get("repeated_blocks") or [])
        if not blocks:
            continue
        block = blocks[0]
        add(f"- `{page['url']}`: {block['item_count']} items at `{block['item_selector']}` "
            f"inside `{block['container_selector']}` ({block['selector_durability']})")
        for field in block["fields"][:12]:
            add(f"  - `{field['field']}`: `{field['selector']}` [{field['attribute']}]")
    add("")

    robots = report["robots"]
    add("## Constraints the scraper must respect")
    add("")
    add(f"- robots.txt: `{robots.get('url')}`, our target path allowed = {robots.get('allowed')}"
        + (f", Crawl-delay {robots['crawl_delay']}s" if robots.get("crawl_delay") else ""))
    add("- Send a real, identifiable User-Agent; do not spoof a browser you are not using.")
    add("- Concurrency: 1-2 requests in flight, with a delay between requests "
        + (f"of at least {robots['crawl_delay']}s" if robots.get("crawl_delay") else "of >= 1s") + ".")
    add("- Exponential backoff with jitter on 429/403/5xx; stop after repeated 403s rather than "
        "rotating identities.")
    add("- Cache raw responses to disk so re-parsing never re-fetches.")
    add("")
    add("## Deliverable")
    add("")
    add("""A single Python package with:

1. `fetch.py` — the transport layer. One client class, `httpx` (HTTP/2 on) or `requests`;
   retry/backoff with jitter; per-host rate limiter; on-disk response cache keyed by URL;
   optional Playwright fallback behind one flag, used only if the HTTP path returns a
   challenge page.
2. `parse.py` — pure functions from `bytes -> list[Record]`. No network calls in here, so
   the parsers are unit-testable against the saved fixtures in `pages/*/bodies/`.
3. `models.py` — a typed record (dataclass or pydantic) with explicit field types and units;
   normalise prices to `(amount: Decimal, currency: str)` and dates to ISO-8601.
4. `crawl.py` — the pagination/enumeration loop: seed URLs, follow pagination until a page
   yields no new IDs, dedupe by stable record id, write JSONL incrementally, and keep a
   `state.json` so an interrupted run resumes instead of restarting.
5. `cli.py` — `scrape <target> --out data.jsonl --max-pages N --delay S --resume`.
6. `tests/` — parser tests that load the captured fixtures and assert on field values, plus one
   test asserting the record count for a known fixture page. No network in tests.
7. `README.md` — the discovered API contract, the fields, and how to re-run.

Rules: fail loudly on a schema change (if an expected key is missing, raise -- do not silently
emit empty fields); log every non-200 with its URL; never parse JSON out of HTML with regex when
an API or an embedded JSON blob is available; no `time.sleep` in the parse layer.""")
    add("")
    add("## Fixtures available for the tests")
    add("")
    for page in report["pages"]:
        add(f"- `{page['dir']}/raw/initial.html` (server HTML), "
            f"`{page['dir']}/raw/rendered.html` (post-JS DOM)")
    body_files = [(g["path_template"], g["sample_body_file"]) for g in first_party
                  if g.get("sample_body_file")][:10]
    for template, body_file in body_files:
        add(f"- `{body_file}` — sample response for `{template}`")
    add("")
    return "\n".join(out)


def artifact_tree(root: Path, limit: int = 120) -> str:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        lines.append(f"{rel}  ({path.stat().st_size:,}B)")
        if len(lines) >= limit:
            lines.append(f"... (+ more files under {root.name}/)")
            break
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def parse_kv(values: list[str] | None, sep: str) -> list[tuple[str, str]]:
    out = []
    for item in values or []:
        if sep not in item:
            raise argparse.ArgumentTypeError(f"expected NAME{sep}VALUE, got {item!r}")
        name, value = item.split(sep, 1)
        out.append((name.strip(), value.strip()))
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webprobe",
        description="Capture a web page's whole request/response surface and analyse it "
                    "into a scraping plan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Pass a listing URL and a detail URL together to get both halves of a scraper "
               "in one report.",
    )
    parser.add_argument("urls", nargs="+", help="one or more URLs to capture")
    parser.add_argument("-o", "--out", default="webprobe-out", help="output directory root")
    parser.add_argument("--goal", default="", help="what the scraper should extract (goes in the brief)")
    parser.add_argument("--target-fields", default="",
                        help="comma-separated fields the scraper must produce")

    group = parser.add_argument_group("capture")
    group.add_argument("--static", action="store_true", help="no browser; stdlib HTTP only")
    group.add_argument("--browser", default="chromium", choices=("chromium", "firefox", "webkit"))
    group.add_argument("--headed", action="store_true", help="show the browser window")
    group.add_argument("--wait", type=int, default=3000, help="extra settle time in ms (default 3000)")
    group.add_argument("--scroll", type=int, default=0, metavar="N",
                       help="scroll N screens to trigger lazy loading / infinite scroll")
    group.add_argument("--click", action="append", metavar="SELECTOR",
                       help="click this selector after load (repeatable)")
    group.add_argument("--timeout", type=int, default=45000, help="per-navigation timeout in ms")
    group.add_argument("--viewport", default="1440x900", help="WxH (default 1440x900)")
    group.add_argument("--ua", default=DEFAULT_UA, help="user agent")
    group.add_argument("--locale", default="en-US")
    group.add_argument("--timezone", default="UTC")
    group.add_argument("--proxy", default="", help="proxy server, e.g. http://host:port")
    group.add_argument("--header", action="append", metavar="NAME:VALUE",
                       help="extra request header (repeatable)")
    group.add_argument("--cookie", action="append", metavar="NAME=VALUE",
                       help="cookie to send (repeatable) -- for studying logged-in pages")
    group.add_argument("--storage-state", default="",
                       help="Playwright storage_state.json to reuse a logged-in session")
    group.add_argument("--block-images", action="store_true", help="skip images/fonts for speed")
    group.add_argument("--no-stealth", action="store_true", help="do not patch webdriver flags")
    group.add_argument("--no-consent", action="store_true", help="do not auto-dismiss cookie banners")
    group.add_argument("--no-screenshots", action="store_true")

    group = parser.add_argument_group("output")
    group.add_argument("--max-body-kb", type=int, default=512, help="per-body save cap (default 512)")
    group.add_argument("--save-all-bodies", action="store_true", help="also save binary bodies")
    group.add_argument("--keep-har", action="store_true", help="keep the raw HAR (can be large)")
    group.add_argument("--redact", action="store_true",
                       help="mask auth headers and cookie values in the reports")

    group = parser.add_argument_group("behaviour")
    group.add_argument("--no-replay", action="store_true",
                       help="skip re-issuing discovered endpoints from plain Python")
    group.add_argument("--replay-post", action="store_true", help="also replay POST endpoints")
    group.add_argument("--replay-limit", type=int, default=10)
    group.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    group.add_argument("--ignore-robots", action="store_true",
                       help="proceed even if robots.txt disallows the URL (your call to make)")
    group.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        width, height = (int(v) for v in args.viewport.lower().split("x"))
        args.viewport = (width, height)
    except Exception:
        print("--viewport wants WxH, e.g. 1440x900", file=sys.stderr)
        return 2
    args.header = parse_kv(args.header, ":")
    args.cookie = parse_kv(args.cookie, "=")
    args.target_fields = [f.strip() for f in args.target_fields.split(",") if f.strip()]

    start_url = args.urls[0]
    host = urllib.parse.urlsplit(start_url).hostname or ""
    if not host:
        print(f"not a URL: {start_url}", file=sys.stderr)
        return 2

    log(f"robots.txt for {host}", args.quiet, "00")
    robots = probe_robots(start_url, args.quiet)
    blocked = [u for u in args.urls if robots.get("fetched") and robots.get("allowed") is False]
    if blocked and not args.ignore_robots:
        print(f"\nrobots.txt at {robots['url']} disallows this path for our user agent.",
              file=sys.stderr)
        for rule in robots.get("matching_rules", [])[:8]:
            print(f"  {rule}", file=sys.stderr)
        print("\nPick a path robots allows, or pass --ignore-robots to proceed on your own "
              "authority.\n", file=sys.stderr)
        return 3
    if robots.get("crawl_delay") and robots["crawl_delay"] > args.delay:
        log(f"robots Crawl-delay is {robots['crawl_delay']}s -- raising --delay to match",
            args.quiet, "!!")
        args.delay = robots["crawl_delay"]

    out_dir = Path(args.out) / f"{slugify(registrable_domain(host), 40)}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"output → {out_dir}", args.quiet, "00")

    sitemaps = probe_sitemaps(robots.get("sitemaps", []), out_dir) if robots.get("sitemaps") else []

    browser_ctx = None
    pages: list[dict] = []
    browser_used = False
    browser_error = None
    try:
        if not args.static:
            try:
                browser_ctx = BrowserCapture(args).__enter__()
                browser_used = True
            except Exception as exc:
                browser_error = str(exc)
                log(f"browser unavailable ({exc}); continuing static-only", args.quiet, "!!")
        for index, url in enumerate(args.urls):
            if index:
                time.sleep(args.delay)
            try:
                pages.append(probe_page(url, out_dir, args, browser_ctx))
            except Exception as exc:
                log(f"page failed: {url}: {type(exc).__name__}: {exc}", args.quiet, "!!")
                pages.append({"url": url, "error": f"{type(exc).__name__}: {exc}",
                              "dir": "(failed)"})
    finally:
        if browser_ctx is not None:
            browser_ctx.__exit__(None, None, None)

    first_party = registrable_domain(host)
    all_records = [r for p in pages for r in (p.get("network") or [])]
    evidence: dict[str, list] = {"header": [], "cookie": [], "url": [], "html": [], "global": []}
    for page in pages:
        for key, values in build_evidence(page).items():
            evidence[key].extend(values)

    log("analysing", args.quiet, "03")
    endpoints = api_candidates(all_records, first_party)
    graphql = graphql_operations(all_records)
    replay: list[dict] = []
    if endpoints and not args.no_replay and not args.static:
        log("replaying endpoints without the browser", args.quiet, "04")
        replay = replay_endpoints(endpoints, args, args.quiet)

    antibot = match_signs(evidence, ANTIBOT_SIGNS, with_severity=True)
    stack = match_signs(evidence, STACK_SIGNS)
    assessment = difficulty_verdict(pages, antibot, replay)

    slim_pages = []
    for page in pages:
        copy = {k: v for k, v in page.items()
                if k not in ("network", "static", "browser", "embedded_state_static",
                             "embedded_state_rendered")}
        copy["static_summary"] = page.get("static_summary")
        copy["embedded_state_static"] = {
            k: truncate(json.dumps(v, default=str), 400) for k, v in
            (page.get("embedded_state_static") or {}).items()}
        copy["embedded_state_rendered"] = {
            k: truncate(json.dumps(v, default=str), 400) for k, v in
            (page.get("embedded_state_rendered") or {}).items()}
        browser = page.get("browser") or {}
        copy["browser"] = {k: v for k, v in browser.items() if k != "rendered_html"}
        slim_pages.append(copy)

    report = {
        "webprobe_version": VERSION,
        "captured_at": now_iso(),
        "site": {"start_url": start_url, "host": host, "registrable_domain": first_party},
        "config": {
            "urls": args.urls, "browser_used": browser_used, "browser_error": browser_error,
            "browser": args.browser, "scroll": args.scroll, "wait_ms": args.wait,
            "user_agent": args.ua, "delay_s": args.delay, "redact": args.redact,
            "static_only": args.static,
        },
        "assessment": assessment,
        "stack": stack,
        "antibot": antibot,
        "api_endpoints": endpoints,
        "graphql": graphql,
        "replay": replay,
        "robots": robots,
        "sitemaps": sitemaps,
        "pages": slim_pages,
        "artifact_tree": "",
    }

    # The full-fidelity page records (with bodies) go in their own file; report.json stays
    # readable by a human and small enough to paste to an agent.
    write_json(out_dir / "network-detail.json",
               {p["url"]: p.get("network", []) for p in pages})
    report["artifact_tree"] = artifact_tree(out_dir)
    write_json(out_dir / "report.json", report)

    # pages[] in the markdown/brief want the analysis dicts, which live on the fat objects
    for slim, fat in zip(report["pages"], pages, strict=False):
        slim["dom_analysis"] = fat.get("dom_analysis")
        slim["static_analysis"] = fat.get("static_analysis")
        slim["embedded_state_static"] = fat.get("embedded_state_static") or {}
        slim["embedded_state_rendered"] = fat.get("embedded_state_rendered") or {}

    write_text(out_dir / "REPORT.md", render_report_md(report))
    write_text(out_dir / "HERMES_BRIEF.md", render_hermes_brief(report, args))
    report["artifact_tree"] = artifact_tree(out_dir)
    write_json(out_dir / "report.json", report)

    if not args.quiet:
        print("", file=sys.stderr)
        print(f"  difficulty : {assessment['difficulty']}", file=sys.stderr)
        print(f"  strategy   : {assessment['recommended_strategy']}", file=sys.stderr)
        print(f"  stack      : {', '.join(s['name'] for s in stack[:6]) or '-'}", file=sys.stderr)
        print(f"  anti-bot   : {', '.join(a['name'] for a in antibot) or 'none detected'}",
              file=sys.stderr)
        print(f"  endpoints  : {len([e for e in endpoints if e['first_party']])} first-party, "
              f"{len(endpoints)} total", file=sys.stderr)
        print(f"  requests   : {len(all_records)}", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"  {out_dir}/REPORT.md", file=sys.stderr)
        print(f"  {out_dir}/HERMES_BRIEF.md   <- hand this to the coding agent", file=sys.stderr)
        print(f"  {out_dir}/report.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
