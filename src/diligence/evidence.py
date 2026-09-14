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
from datetime import datetime, timezone
from urllib.parse import urlparse

# Third-party sources treated as authoritative for security claims: vulnerability
# registries, regulators, and outlets with editorial standards for breach reporting.
AUTHORITATIVE_DOMAINS: frozenset[str] = frozenset(
    {
        "nvd.nist.gov", "cve.mitre.org", "cwe.mitre.org", "cisa.gov", "us-cert.gov",
        "nist.gov", "enisa.europa.eu", "europa.eu", "ico.org.uk", "sec.gov", "ftc.gov",
        "reuters.com", "bloomberg.com", "apnews.com", "ft.com", "wsj.com",
        "krebsonsecurity.com", "bleepingcomputer.com", "theregister.com",
        "arstechnica.com", "wired.com", "schneier.com",
        "cloudsecurityalliance.org",
    }
)

TIER_LABELS = {1: "vendor-primary", 2: "authoritative-third-party", 3: "secondary"}


def registrable(host: str) -> str:
    """Crude eTLD+1. Good enough for tiering; not a public-suffix implementation."""
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle the common two-label public suffixes we are likely to meet.
    if parts[-2] in {"co", "com", "org", "gov", "ac", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_tier(url: str, vendor_domain: str | None) -> int:
    """T1 vendor primary, T2 authoritative third party, T3 secondary commentary."""
    host = urlparse(url).netloc.lower()
    reg = registrable(host)
    if vendor_domain and reg == registrable(vendor_domain):
        return 1
    if reg in AUTHORITATIVE_DOMAINS or host in AUTHORITATIVE_DOMAINS:
        return 2
    if reg.endswith(".gov") or reg.endswith(".europa.eu"):
        return 2
    return 3


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
    passage: str
    matched_keywords: list[str] = field(default_factory=list)
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    raw_chars: int = 0
    selected_chars: int = 0

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "unknown")

    def for_prompt(self) -> str:
        return (
            f"[{self.id}] ({self.tier_label}) {self.title}\n"
            f"URL: {self.url}\n"
            f"PASSAGE: {self.passage}"
        )


def build_evidence(
    result: dict,
    *,
    eid: str,
    vendor_domain: str | None,
    keywords: tuple[str, ...],
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

    return Evidence(
        id=eid,
        url=url,
        title=_normalise(result.get("title") or "Untitled")[:200],
        domain=urlparse(url).netloc.lower(),
        tier=classify_tier(url, vendor_domain),
        passage=passage,
        matched_keywords=matched,
        raw_chars=len(source_text),
        selected_chars=len(passage),
    )
