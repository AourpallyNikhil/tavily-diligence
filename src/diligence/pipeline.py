"""The diligence pipeline: retrieve -> select -> extract -> verify -> report.

Fixed control flow, on purpose. The starter agent hands a tool to a model and lets
it decide what to do; that is the right shape for open-ended research and the wrong
shape for a compliance artefact. A diligence report that takes a different path on
every run cannot be audited, diffed, or evaluated against a fixed question set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from tavily import TavilyClient

from .checklist import CHECKLIST, Dimension
from .config import Config
from .evidence import Evidence
from .findings import Finding, Status, extract_findings
from .llm import LLM, Usage
from .retrieve import Retriever, RetrievalStats
from .tracing import init_tracing, span
from .verify import verify_findings


@dataclass
class DimensionResult:
    dimension: Dimension
    findings: list[Finding] = field(default_factory=list)
    pool: list[Evidence] = field(default_factory=list)
    stats: RetrievalStats | None = None

    @property
    def supported(self) -> list[Finding]:
        return [f for f in self.findings if f.status is Status.SUPPORTED]

    @property
    def unverified(self) -> list[Finding]:
        return [f for f in self.findings if f.status is Status.UNVERIFIED]


@dataclass
class Report:
    vendor: str
    vendor_domain: str | None
    results: list[DimensionResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0

    @property
    def all_findings(self) -> list[Finding]:
        return [f for r in self.results for f in r.findings]

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "vendor_domain": self.vendor_domain,
            "elapsed_s": round(self.elapsed_s, 2),
            "usage": {
                "calls": self.usage.calls,
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "by_model": self.usage.by_model,
            },
            "dimensions": [
                {
                    "key": r.dimension.key,
                    "title": r.dimension.title,
                    "retrieval": {
                        "queries_run": r.stats.queries_run if r.stats else 0,
                        "results_seen": r.stats.results_seen if r.stats else 0,
                        "evidence_kept": r.stats.evidence_kept if r.stats else 0,
                        "tier_counts": r.stats.tier_counts if r.stats else {},
                    },
                    "evidence": [
                        {
                            "id": e.id,
                            "url": e.url,
                            "title": e.title,
                            "tier": e.tier,
                            "tier_label": e.tier_label,
                            "matched_keywords": e.matched_keywords,
                            "raw_chars": e.raw_chars,
                            "selected_chars": e.selected_chars,
                            "passage": e.passage,
                        }
                        for e in r.pool
                    ],
                    "findings": [f.to_dict() for f in r.findings],
                }
                for r in self.results
            ],
        }


def run_diligence(
    vendor: str,
    *,
    vendor_domain: str | None,
    config: Config,
    dimensions: tuple[Dimension, ...] = CHECKLIST,
    verify: bool = True,
) -> Report:
    init_tracing()
    started = time.time()

    usage = Usage()
    llm = LLM(config.llm_api_key, config.base_url, usage=usage)
    retriever = Retriever(TavilyClient(api_key=config.tavily_api_key), config.max_results_per_query)

    report = Report(vendor=vendor, vendor_domain=vendor_domain, usage=usage)

    with span(
        "diligence.report",
        {"diligence.vendor": vendor, "diligence.vendor_domain": vendor_domain or ""},
    ):
        for dimension in dimensions:
            with span("diligence.dimension", {"diligence.dimension": dimension.key}):
                pool, stats = retriever.gather(
                    vendor, dimension, vendor_domain=vendor_domain
                )
                findings = extract_findings(
                    llm, config.writer_model, dimension, pool, subject=vendor
                )
                if verify:
                    findings = verify_findings(
                        llm, config.verifier_model, findings, pool
                    )
                report.results.append(
                    DimensionResult(
                        dimension=dimension, findings=findings, pool=pool, stats=stats
                    )
                )

    report.elapsed_s = time.time() - started
    return report
