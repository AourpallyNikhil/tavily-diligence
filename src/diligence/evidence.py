"""Evidence records, authority tiering, and deterministic passage selection.

The evidence pool is a hard boundary. The writer model is shown *only* these
records -- never the open question phrased as something to answer from knowledge.
There is therefore nothing to answer from except retrieved text, which is the
structural fix for the starter agent's largest groundedness gap: nothing in it
required the agent to search at all.

Passage selection exists because of a measured tension. Tavily's `content` field
averaged ~1.3k chars in our probes; `include_raw_content` raised that to ~17.5k
(13.7x). The extra text is what actually contains the supporting fact, but five
results across three dimensions at that size is ~65k tokens of context -- which
recreates the context-bloat problem from the other direction. So we fetch the full
page and then select from it.

Selection is deterministic keyword scoring rather than embeddings. Three reasons:
it is reproducible (evals compare cleanly across runs), it is explainable (the
selected passage can be traced to the terms that selected it), and it costs
nothing. Embedding-based selection is the obvious upgrade; see README limitations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Source roles.
#
# The first version of this module asked "is this domain trustworthy?" and
# answered it with a 22-entry allowlist. That was wrong at the root, and a live
# run showed exactly how: Mandiant's forensic report on UNC5537 -- the definitive
# account of the 2024 Snowflake campaign -- was tagged `secondary`, because it
# publishes under cloud.google.com and my eTLD+1 logic collapsed that to
# google.com. The Canadian Centre for Cyber Security advisory was tagged
# `secondary` too, because the .gov special case was US-only. Both ranked below
# marketing blogs that got the attribution wrong.
#
# Authority is not a property of a domain. It is a relation between a source and
# a *claim type*. A vendor is authoritative about its own certifications and its
# own subprocessor list; it is not a neutral party about its own culpability in
# a breach. So sources are classified into roles, and each dimension declares
# which roles it will accept -- see checklist.py.
# ---------------------------------------------------------------------------


class Role(str, Enum):
    VENDOR = "vendor-primary"
    REGULATOR = "regulator-cert"
    FORENSICS = "forensics"
    REGISTRY = "vuln-registry"
    PRESS = "press-of-record"
    DERIVATIVE = "derivative"


# Accountability is the common thread in the non-derivative roles: a named party
# that bears a cost for being wrong. That is why government and CERT domains are
# a usable proxy -- not because governments are insightful, but because they are
# answerable. Content marketing is answerable to nobody.

REGULATOR_SUFFIXES: tuple[str, ...] = (
    ".gov", ".gov.uk", ".gov.au", ".gov.sg", ".gov.in", ".gc.ca", ".govt.nz",
    ".europa.eu", ".gov.ie", ".go.jp", ".gov.br",
)
REGULATOR_HOSTS: frozenset[str] = frozenset({
    "cisa.gov", "nist.gov", "nvd.nist.gov", "us-cert.gov", "ic3.gov",
    "enisa.europa.eu", "ico.org.uk", "ncsc.gov.uk", "cyber.gc.ca",
    "cyber.gov.au", "sec.gov", "ftc.gov", "bsi.bund.de", "cert.europa.eu",
    "csirt.cz", "cert.pl",
})

# Incident-response and threat-intelligence publishers. Several live on a parent
# company's domain, so these are matched host+path, not by registrable domain --
# the Mandiant case is the reason this function takes a full URL.
FORENSICS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("cloud.google.com", "/blog/topics/threat-intelligence"),
    ("www.mandiant.com", ""),
    ("mandiant.com", ""),
    ("www.crowdstrike.com", "/blog"),
    ("unit42.paloaltonetworks.com", ""),
    ("blog.talosintelligence.com", ""),
    ("www.microsoft.com", "/en-us/security/blog"),
    ("msrc.microsoft.com", ""),
    ("securelist.com", ""),
    ("www.volexity.com", "/blog"),
    ("www.huntress.com", "/blog"),
    ("rapid7.com", "/blog"),
    ("www.sentinelone.com", "/labs"),
)

REGISTRY_HOSTS: frozenset[str] = frozenset({
    "nvd.nist.gov", "cve.mitre.org", "cwe.mitre.org", "www.cve.org", "cve.org",
    "osv.dev", "github.com",  # github only via advisory paths, checked below
})

PRESS_HOSTS: frozenset[str] = frozenset({
    "reuters.com", "bloomberg.com", "apnews.com", "ft.com", "wsj.com",
    "nytimes.com", "washingtonpost.com",
    "krebsonsecurity.com", "bleepingcomputer.com", "theregister.com",
    "arstechnica.com", "wired.com", "schneier.com", "therecord.media",
    "cyberscoop.com", "darkreading.com",
})

# Retained for display and for the eval's tier arithmetic.
# 1 = the vendor itself, 2 = an independent accountable party, 3 = derivative.
ROLE_TIER: dict[Role, int] = {
    Role.VENDOR: 1,
    Role.REGULATOR: 2,
    Role.FORENSICS: 2,
    Role.REGISTRY: 2,
    Role.PRESS: 2,
    Role.DERIVATIVE: 3,
}

TIER_LABELS = {1: "vendor-primary", 2: "independent", 3: "derivative"}


def registrable(host: str) -> str:
    """Crude eTLD+1. Not a public-suffix implementation; good enough for vendor
    matching, and deliberately NOT used for authority decisions any more."""
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if parts[-2] in {"co", "com", "org", "gov", "ac", "net", "gc", "go", "govt"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_role(url: str, vendor_domain: str | None) -> Role:
    """Classify a source by the role it plays, from the full URL.

    Takes the URL rather than the domain because several authoritative
    publishers sit on a path under a parent company's host.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    bare = host[4:] if host.startswith("www.") else host
    path = parsed.path or ""

    if vendor_domain and registrable(host) == registrable(vendor_domain):
        return Role.VENDOR

    for fhost, fprefix in FORENSICS_PREFIXES:
        if bare == (fhost[4:] if fhost.startswith("www.") else fhost) and path.startswith(fprefix):
            return Role.FORENSICS

    if bare in REGULATOR_HOSTS or host in REGULATOR_HOSTS:
        return Role.REGULATOR
    if any(bare.endswith(sfx) for sfx in REGULATOR_SUFFIXES):
        return Role.REGULATOR

    if bare in REGISTRY_HOSTS and (bare != "github.com" or "/advisories" in path):
        return Role.REGISTRY

    if registrable(host) in PRESS_HOSTS or bare in PRESS_HOSTS:
        return Role.PRESS

    return Role.DERIVATIVE


def classify_tier(url: str, vendor_domain: str | None) -> int:
    return ROLE_TIER[classify_role(url, vendor_domain)]


_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _chunk(text: str, size: int = 700) -> list[str]:
    """Split on sentence-ish boundaries, then group into ~`size`-char windows."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) + 1 > size and buf:
            chunks.append(buf.strip())
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        chunks.append(buf.strip())
    return chunks


def select_passages(
    text: str,
    keywords: tuple[str, ...],
    *,
    max_chunks: int = 3,
    max_chars: int = 1800,
) -> tuple[str, list[str]]:
    """Return (selected_text, matched_keywords).

    Scores each chunk by how many *distinct* dimension keywords it contains, which
    favours passages discussing the topic over passages that repeat one term. Ties
    break toward earlier chunks. Selected chunks are re-emitted in document order
    so the passage still reads coherently.
    """
    text = _normalise(text)
    if not text:
        return "", []

    chunks = _chunk(text)
    scored: list[tuple[int, int, int, set[str]]] = []  # (-score, index, len, hits)
    for i, c in enumerate(chunks):
        low = c.lower()
        hits = {k for k in keywords if k in low}
        if hits:
            scored.append((-len(hits), i, len(c), hits))

    if not scored:
        return text[:max_chars], []

    scored.sort()
    picked = sorted(scored[:max_chunks], key=lambda t: t[1])

    out: list[str] = []
    matched: set[str] = set()
    total = 0
    for _, i, _, hits in picked:
        c = chunks[i]
        if total + len(c) > max_chars:
            c = c[: max(0, max_chars - total)]
        if not c:
            break
        out.append(c)
        matched |= hits
        total += len(c)

    return " […] ".join(out), sorted(matched)


@dataclass
class Evidence:
    """One retrieved source, normalised. The only thing the writer ever sees."""

    id: str
    url: str
    title: str
    domain: str
    tier: int
    role: str
    passage: str
    matched_keywords: list[str] = field(default_factory=list)
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    raw_chars: int = 0
    selected_chars: int = 0
    # Position in Tavily's own result ordering. Used as the final sort tiebreak,
    # because Tavily's relevance ranking is a genuine signal and keyword density
    # is not -- see retrieve.py.
    rank: int = 0

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "unknown")

    def for_prompt(self) -> str:
        return (
            f"[{self.id}] ({self.role}) {self.title}\n"
            f"URL: {self.url}\n"
            f"PASSAGE: {self.passage}"
        )


def build_evidence(
    result: dict,
    *,
    eid: str,
    vendor_domain: str | None,
    keywords: tuple[str, ...],
    rank: int = 0,
) -> Evidence | None:
    """Normalise one Tavily result into an Evidence record.

    Prefers `raw_content` (full page text) and selects from it; falls back to
    `content` (Tavily's own extract) when raw content is unavailable, which our
    probes showed happens for a minority of results.
    """
    url = result.get("url") or ""
    if not url:
        return None

    raw = result.get("raw_content") or ""
    fallback = result.get("content") or ""
    source_text = raw or fallback
    if not source_text.strip():
        return None

    passage, matched = select_passages(source_text, keywords)
    if not passage.strip():
        return None

    role = classify_role(url, vendor_domain)
    return Evidence(
        id=eid,
        url=url,
        title=_normalise(result.get("title") or "Untitled")[:200],
        domain=urlparse(url).netloc.lower(),
        tier=ROLE_TIER[role],
        role=role.value,
        passage=passage,
        matched_keywords=matched,
        raw_chars=len(source_text),
        selected_chars=len(passage),
        rank=rank,
    )
