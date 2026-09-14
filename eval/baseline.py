"""Baseline: the starter agent's approach, reimplemented for measurement.

This is what we are comparing against, and it is built to be *faithful* rather
than weak:

  - Tavily with the supplied defaults (basic depth, no raw content, no domain
    steering, no tiering, no passage selection) -- exactly `TavilySearch()` in
    the starter.
  - Citation by instruction only: "include source URLs when available", the
    starter's own system prompt language.
  - No citation invariant. No verification gate. Nothing enforced in code.

It is given the same structured output shape as the real pipeline. That is
deliberate: holding the output format constant isolates the variable we actually
claim credit for, which is *enforcement*, not formatting. A baseline that emitted
prose would score worse for reasons that have nothing to do with the thesis.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from tavily import TavilyClient

from diligence.checklist import CHECKLIST, Dimension
from diligence.config import Config
from diligence.llm import LLM, Usage
from diligence.tracing import init_tracing, span

STARTER_SYSTEM = """You are a concise research assistant.
Use the search results when you need current or factual web information.
Answer the user's question directly and include source URLs when available.

Return ONLY JSON: {"findings": [{"claim": "<one sentence>", "sources": ["<url>", ...]}]}"""


@dataclass
class BaselineFinding:
    dimension: str
    claim: str
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"dimension": self.dimension, "claim": self.claim, "sources": self.sources}


@dataclass
class BaselineReport:
    vendor: str
    findings: list[BaselineFinding] = field(default_factory=list)
    sources_seen: dict[str, str] = field(default_factory=dict)  # url -> content
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "elapsed_s": round(self.elapsed_s, 2),
            "usage": {
                "calls": self.usage.calls,
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
            "findings": [f.to_dict() for f in self.findings],
        }


def run_baseline(
    vendor: str,
    *,
    config: Config,
    dimensions: tuple[Dimension, ...] = CHECKLIST,
) -> BaselineReport:
    init_tracing()
    started = time.time()

    usage = Usage()
    llm = LLM(config.llm_api_key, config.base_url, usage=usage)
    tavily = TavilyClient(api_key=config.tavily_api_key)
    report = BaselineReport(vendor=vendor, usage=usage)

    with span("baseline.report", {"diligence.vendor": vendor}):
        for dimension in dimensions:
            # Starter-equivalent retrieval: one unsteered query, Tavily defaults.
            query = f"{vendor} {dimension.title}"
            with span("baseline.tavily.search", {"tavily.query": query}) as sp:
                try:
                    payload = tavily.search(query=query, max_results=5)
                    results = payload.get("results", []) or []
                except Exception as exc:
                    sp.set_attribute("tavily.error", str(exc)[:200])
                    results = []
                sp.set_attribute("tavily.results", len(results))

            if not results:
                continue

            blob = "\n\n".join(
                f"{r.get('title','')}\nURL: {r.get('url','')}\n{r.get('content','')}"
                for r in results
            )
            for r in results:
                if r.get("url"):
                    report.sources_seen[r["url"]] = r.get("content", "") or ""

            try:
                data = llm.chat_json(
                    model=config.writer_model,
                    system=STARTER_SYSTEM,
                    user=f"{dimension.question}\n\nSearch results:\n{blob}\n\nReturn JSON only.",
                    span_name=f"baseline.answer.{dimension.key}",
                    attributes={"diligence.dimension": dimension.key},
                )
            except Exception:
                continue

            for item in (data.get("findings", []) if isinstance(data, dict) else []):
                if not isinstance(item, dict):
                    continue
                claim = str(item.get("claim", "")).strip()
                if not claim:
                    continue
                report.findings.append(
                    BaselineFinding(
                        dimension=dimension.key,
                        claim=claim,
                        sources=[str(s) for s in (item.get("sources") or [])],
                    )
                )

    report.elapsed_s = time.time() - started
    return report
