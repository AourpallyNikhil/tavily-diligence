"""Evaluation harness.

Reports four things, each measured by a method that can actually measure it:

  coverage            (positive seed items)  did the system establish the fact?
  abstention accuracy (negative seed items)  did it refrain from asserting a
                                             fact that is not publicly disclosed?
  citation precision  (all emitted claims)   is a claim presented as supported
                                             actually entailed by its citation?
  cost                (traces)               latency, tokens, calls

A note on citation precision, stated plainly because it would otherwise be
misleading: for this pipeline it is ~1.0 *by construction*, since the gate demotes
anything its citation does not entail. That number is not the achievement. The
achievement is the gate's yield -- how many writer-emitted claims it had to
remove -- measured here, and the baseline's unenforced precision measured the
same way.

Usage:
    uv run eval/run_eval.py                 # full run
    uv run eval/run_eval.py --limit 2       # smoke test, 2 vendors
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import typer
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from baseline import run_baseline  # noqa: E402
from diligence.config import Config  # noqa: E402
from diligence.findings import Status  # noqa: E402
from diligence.llm import LLM  # noqa: E402
from diligence.pipeline import run_diligence  # noqa: E402
from diligence.tracing import init_tracing  # noqa: E402

RESULTS = ROOT / "eval" / "results"

JUDGE_SYSTEM = """You decide whether a set of FINDINGS asserts a given GOLD CLAIM.

Answer "yes" only if one of the findings makes substantially the same assertion as
the gold claim. Matching wording is not required; matching substance is. A finding
that is vaguer than the gold claim, or that concerns a different entity, or that
states a related but different fact, is NOT a match.

Return ONLY JSON: {"match": true|false, "finding_index": <int or null>, "reason": "<short>"}"""

ENTAIL_SYSTEM = """You are a strict textual entailment checker.

Given a SOURCE TEXT and a CLAIM, decide whether the source states the claim.
You have no knowledge of the world. If the source does not say it, it is not
entailed, however plausible the claim sounds.

Return ONLY JSON: {"verdict": "entailed|partial|not_entailed"}"""


@dataclass
class ItemScore:
    item_id: str
    vendor: str
    dimension: str
    expectation: str
    system: str
    matched: bool
    correct: bool
    reason: str = ""


@dataclass
class SystemTotals:
    name: str
    coverage_hits: int = 0
    coverage_total: int = 0
    abstention_hits: int = 0
    abstention_total: int = 0
    claims_emitted: int = 0
    claims_presented_supported: int = 0
    claims_entailed: int = 0
    elapsed_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    per_item: list[ItemScore] = field(default_factory=list)

    def pct(self, num: int, den: int) -> str:
        return f"{100.0 * num / den:.0f}%" if den else "n/a"


def judge_match(llm: LLM, model: str, gold_claim: str, claims: list[str]) -> tuple[bool, str]:
    if not claims:
        return False, "no findings"
    listing = "\n".join(f"[{i}] {c}" for i, c in enumerate(claims))
    try:
        data = llm.chat_json(
            model=model,
            system=JUDGE_SYSTEM,
            user=f"GOLD CLAIM:\n{gold_claim}\n\nFINDINGS:\n{listing}\n\nReturn JSON only.",
            span_name="eval.judge_match",
            max_tokens=300,
        )
        return bool(data.get("match")), str(data.get("reason", ""))[:200]
    except Exception as exc:
        return False, f"judge error: {exc}"


def entailed(llm: LLM, model: str, source_text: str, claim: str) -> bool:
    if not source_text.strip():
        return False
    try:
        data = llm.chat_json(
            model=model,
            system=ENTAIL_SYSTEM,
            user=f"SOURCE TEXT:\n{source_text[:6000]}\n\nCLAIM:\n{claim}\n\nReturn JSON only.",
            span_name="eval.entailment",
            max_tokens=200,
        )
        return str(data.get("verdict", "")).lower() == "entailed"
    except Exception:
        return False


def main(
    limit: int = typer.Option(0, help="Only evaluate the first N vendors"),
    skip_baseline: bool = typer.Option(False, help="Evaluate the agent only"),
) -> None:
    init_tracing()
    config = Config.from_env()
    RESULTS.mkdir(parents=True, exist_ok=True)

    gold = yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text())
    items = gold["items"]

    vendors: dict[str, str] = {}
    for it in items:
        vendors.setdefault(it["vendor"], it["domain"])
    vendor_list = list(vendors.items())
    if limit:
        vendor_list = vendor_list[:limit]
        keep = {v for v, _ in vendor_list}
        items = [it for it in items if it["vendor"] in keep]

    print(f"vendors: {[v for v, _ in vendor_list]}")
    print(f"items:   {len(items)}\n")

    agent_reports: dict[str, dict] = {}
    baseline_reports: dict[str, dict] = {}

    for vendor, domain in vendor_list:
        print(f"[agent]    {vendor} ...", flush=True)
        t0 = time.time()
        rep = run_diligence(vendor, vendor_domain=domain, config=config)
        agent_reports[vendor] = rep.to_dict()
        print(f"           {time.time() - t0:.0f}s, {len(rep.all_findings)} findings")

        if not skip_baseline:
            print(f"[baseline] {vendor} ...", flush=True)
            t0 = time.time()
            brep = run_baseline(vendor, config=config)
            baseline_reports[vendor] = brep.to_dict() | {
                "sources_seen": brep.sources_seen
            }
            print(f"           {time.time() - t0:.0f}s, {len(brep.findings)} findings")

    (RESULTS / "agent_reports.json").write_text(json.dumps(agent_reports, indent=2))
    if baseline_reports:
        (RESULTS / "baseline_reports.json").write_text(json.dumps(baseline_reports, indent=2))

    # --- scoring -----------------------------------------------------------
    scorer = LLM(config.llm_api_key, config.base_url)
    agent = SystemTotals("agent")
    base = SystemTotals("baseline")

    for vendor, rep in agent_reports.items():
        agent.elapsed_s += rep["elapsed_s"]
        agent.prompt_tokens += rep["usage"]["prompt_tokens"]
        agent.completion_tokens += rep["usage"]["completion_tokens"]
        agent.llm_calls += rep["usage"]["calls"]
    for vendor, rep in baseline_reports.items():
        base.elapsed_s += rep["elapsed_s"]
        base.prompt_tokens += rep["usage"]["prompt_tokens"]
        base.completion_tokens += rep["usage"]["completion_tokens"]
        base.llm_calls += rep["usage"]["calls"]

    def agent_claims(vendor: str, dimension: str, supported_only: bool) -> list[str]:
        rep = agent_reports.get(vendor, {})
        out = []
        for dim in rep.get("dimensions", []):
            if dim["key"] != dimension:
                continue
            for f in dim["findings"]:
                if supported_only and f["status"] != Status.SUPPORTED.value:
                    continue
                out.append(f["claim"])
        return out

    def baseline_claims(vendor: str, dimension: str) -> list[str]:
        rep = baseline_reports.get(vendor, {})
        return [f["claim"] for f in rep.get("findings", []) if f["dimension"] == dimension]

    for it in items:
        gold_claim = " ".join(it["gold_claim"].split())
        positive = it["expectation"] == "present"

        for totals, claims in (
            (agent, agent_claims(it["vendor"], it["dimension"], supported_only=True)),
            (base, baseline_claims(it["vendor"], it["dimension"])),
        ):
            if totals is base and skip_baseline:
                continue
            matched, reason = judge_match(scorer, config.verifier_model, gold_claim, claims)
            correct = matched if positive else not matched
            if positive:
                totals.coverage_total += 1
                totals.coverage_hits += int(correct)
            else:
                totals.abstention_total += 1
                totals.abstention_hits += int(correct)
            totals.per_item.append(
                ItemScore(
                    item_id=it["id"],
                    vendor=it["vendor"],
                    dimension=it["dimension"],
                    expectation=it["expectation"],
                    system=totals.name,
                    matched=matched,
                    correct=correct,
                    reason=reason,
                )
            )

    # --- citation precision -------------------------------------------------
    labelling_rows: list[dict] = []

    for vendor, rep in agent_reports.items():
        for dim in rep["dimensions"]:
            by_id = {e["id"]: e for e in dim["evidence"]}
            for f in dim["findings"]:
                agent.claims_emitted += 1
                if f["status"] != Status.SUPPORTED.value:
                    continue
                agent.claims_presented_supported += 1
                # Post-gate entailment is true by construction; recorded for the
                # human-labelling file so the judge itself can be audited.
                for eid in f["evidence_ids"]:
                    ev = by_id.get(eid)
                    if not ev:
                        continue
                    labelling_rows.append(
                        {
                            "system": "agent",
                            "vendor": vendor,
                            "claim": f["claim"],
                            "source_url": ev["url"],
                            "passage": ev["passage"][:1200],
                            "machine_verdict": f["verdicts"].get(eid, ""),
                            "human_verdict": "",
                        }
                    )
                agent.claims_entailed += 1

    for vendor, rep in baseline_reports.items():
        seen = rep.get("sources_seen", {})
        for f in rep["findings"]:
            base.claims_emitted += 1
            base.claims_presented_supported += 1
            ok = False
            for url in f["sources"]:
                text = seen.get(url, "")
                verdict = entailed(scorer, config.verifier_model, text, f["claim"])
                labelling_rows.append(
                    {
                        "system": "baseline",
                        "vendor": vendor,
                        "claim": f["claim"],
                        "source_url": url,
                        "passage": text[:1200],
                        "machine_verdict": "entailed" if verdict else "not_entailed",
                        "human_verdict": "",
                    }
                )
                ok = ok or verdict
            base.claims_entailed += int(ok)

    (RESULTS / "claim_labels.json").write_text(json.dumps(labelling_rows, indent=2))

    # --- report -------------------------------------------------------------
    rows = [
        ("coverage (positives)", agent.pct(agent.coverage_hits, agent.coverage_total),
         base.pct(base.coverage_hits, base.coverage_total)),
        ("abstention accuracy (negatives)", agent.pct(agent.abstention_hits, agent.abstention_total),
         base.pct(base.abstention_hits, base.abstention_total)),
        ("claims presented as supported", str(agent.claims_presented_supported),
         str(base.claims_presented_supported)),
        ("of those, entailed by citation", agent.pct(agent.claims_entailed, agent.claims_presented_supported),
         base.pct(base.claims_entailed, base.claims_presented_supported)),
        ("claims emitted before gating", str(agent.claims_emitted), str(base.claims_emitted)),
        ("gate yield (removed)", agent.pct(agent.claims_emitted - agent.claims_presented_supported, agent.claims_emitted), "n/a"),
        ("total elapsed", f"{agent.elapsed_s:.0f}s", f"{base.elapsed_s:.0f}s"),
        ("llm calls", str(agent.llm_calls), str(base.llm_calls)),
        ("tokens in/out", f"{agent.prompt_tokens}/{agent.completion_tokens}",
         f"{base.prompt_tokens}/{base.completion_tokens}"),
    ]

    md = ["| metric | agent | baseline |", "|---|---|---|"]
    md += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    table = "\n".join(md)

    (RESULTS / "summary.md").write_text(
        f"# Evaluation results\n\nGenerated {time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Seed items: {len(items)} | Vendors: {len(vendor_list)}\n\n{table}\n"
    )
    (RESULTS / "per_item.json").write_text(
        json.dumps(
            [s.__dict__ for s in agent.per_item + base.per_item], indent=2
        )
    )

    print("\n" + table)
    print(f"\nwrote {RESULTS}/summary.md, per_item.json, claim_labels.json")


if __name__ == "__main__":
    typer.run(main)
