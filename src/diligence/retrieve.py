"""Tavily retrieval, steered and tiered.

Two passes per dimension:

  1. A *primary-source pass* restricted to the vendor's own domain. This exists
     because of a measured failure: the unsteered query
     "Cloudflare subprocessors list data residency" returned Atlassian, OneTrust
     and HumanSecurity -- every one of them a competitor's subprocessor page
     ranking well for the generic phrase, and not one of them Cloudflare. Without
     domain steering, "where does this vendor's data live" silently becomes
     "whose subprocessor page has the best SEO".

  2. An *open pass* for third-party corroboration -- regulators, CVE registries,
     and press. A vendor is not a neutral source about its own breach history, so
     the open pass is what makes dimension 2 meaningful at all.

Both passes request `include_raw_content`, because Tavily's `content` field is a
short extract (~1.3k chars mean in our probes) that frequently gestures at a fact
without containing it. Selection down to a usable size happens in evidence.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from tavily import TavilyClient

from .checklist import Dimension
from .evidence import Evidence, build_evidence
from .tracing import span


@dataclass
class RetrievalStats:
    queries_run: int = 0
    results_seen: int = 0
    evidence_kept: int = 0
    tier_counts: dict[int, int] | None = None


class Retriever:
    def __init__(self, client: TavilyClient, max_results: int = 5) -> None:
        self._client = client
        self._max_results = max_results

    def _search(self, query: str, *, include_domains: list[str] | None = None) -> list[dict]:
        kwargs: dict = {
            "query": query,
            "max_results": self._max_results,
            "search_depth": "advanced",
            "include_raw_content": True,
        }
        if include_domains:
            kwargs["include_domains"] = include_domains

        with span(
            "tavily.search",
            {
                "tavily.query": query,
                "tavily.include_domains": ",".join(include_domains or []),
                "tavily.max_results": self._max_results,
            },
        ) as sp:
            try:
                payload = self._client.search(**kwargs)
            except Exception as exc:  # network / quota / auth
                sp.set_attribute("tavily.error", str(exc)[:300])
                return []
            results = payload.get("results", []) or []
            sp.set_attribute("tavily.results", len(results))
            return results

    def gather(
        self,
        vendor: str,
        dimension: Dimension,
        *,
        vendor_domain: str | None,
    ) -> tuple[list[Evidence], RetrievalStats]:
        """Run both passes for one dimension and return a deduplicated pool."""
        stats = RetrievalStats(tier_counts={1: 0, 2: 0, 3: 0})
        raw: list[dict] = []

        queries = [q.format(vendor=vendor) for q in dimension.queries]

        if vendor_domain:
            # Two steered passes, not one: a single primary-source query was losing
            # to the open passes in the merged pool, which is how another company's
            # subprocessor page became the best-supported "evidence" about ours.
            for q in queries[:2]:
                raw += self._search(q, include_domains=[vendor_domain])
                stats.queries_run += 1

        for q in queries:
            raw += self._search(q)
            stats.queries_run += 1

        stats.results_seen = len(raw)

        pool: list[Evidence] = []
        seen_urls: set[str] = set()
        for result in raw:
            url = (result.get("url") or "").split("#")[0].rstrip("/")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            ev = build_evidence(
                result,
                eid=f"{dimension.key[:4]}-{len(pool) + 1}",
                vendor_domain=vendor_domain,
                keywords=dimension.keywords,
                rank=len(seen_urls),
                temporal=dimension.temporal,
                max_chunks=dimension.max_chunks,
                max_chars=dimension.max_chars,
            )
            if ev is None:
                continue
            pool.append(ev)
            stats.tier_counts[ev.tier] = stats.tier_counts.get(ev.tier, 0) + 1

        # The writer reads in order, so this ordering is context priority.
        #
        # The previous tiebreak was -len(matched_keywords), which was a mistake
        # with a measurable cost: keyword density is precisely what SEO content
        # is optimised to maximise. On a live Snowflake run it ranked marketing
        # blogs 10th-14th of 21 and the Canadian Centre for Cyber Security
        # advisory *last*, because the CERT writes sparse factual prose and the
        # blogs repeat "breach" and "incident" constantly. The ranker rewarded
        # exactly the sources it should have discounted.
        #
        # Keyword overlap is now capped at 3 -- enough to separate an on-topic
        # page from an off-topic one, not enough for stuffing to dominate -- and
        # the final tiebreak is Tavily's own relevance rank, which is a real
        # signal rather than one the source controls.
        pool.sort(key=lambda e: (e.tier, -min(len(e.matched_keywords), 3), e.rank))
        stats.evidence_kept = len(pool)
        return pool, stats
