"""Finding extraction, and the invariant that makes the whole thing trustworthy.

The starter agent's citation mechanism is a sentence in a system prompt:
"include source URLs when available". That is a request, not a constraint, and an
answer with no citations passes silently.

Here, citation is an invariant enforced in code after the model has spoken:

    a finding with no resolvable evidence ids IS unverified

`enforce_citation_invariant` is the function that does it, and it is the single
most important function in this repository. It cannot be prompted away, it cannot
be talked out of its position by a persuasive model, and it is unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .checklist import Dimension
from .evidence import Evidence
from .llm import LLM


class Status(str, Enum):
    SUPPORTED = "supported"
    UNVERIFIED = "unverified"


@dataclass
class Finding:
    dimension: str
    claim: str
    status: Status
    evidence_ids: list[str] = field(default_factory=list)
    # Populated by verify.py. None means the verification gate has not run.
    verdicts: dict[str, str] = field(default_factory=dict)
    demoted_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "claim": self.claim,
            "status": self.status.value,
            "evidence_ids": list(self.evidence_ids),
            "verdicts": dict(self.verdicts),
            "demoted_reason": self.demoted_reason,
        }


WRITER_SYSTEM = """You are a security due-diligence analyst producing auditable findings.

You will be given a SUBJECT, NUMBERED EVIDENCE PASSAGES retrieved from the web, and
one question.

Absolute rules:
0. Every claim must be a fact about the SUBJECT'S OWN security posture. Retrieved
   pages frequently mention the subject as a third party's vendor -- another
   company's subprocessor list naming the subject is a fact about THAT company, not
   about the subject. Discard those. If a passage is not about the subject itself,
   it is not evidence for this report.
1. Every claim you make must be stated in one of the evidence passages. Do not use
   anything you know from training. If the passages do not say it, you do not know it.
2. Every claim must cite the evidence ids it comes from, e.g. ["cert-1", "cert-3"].
3. Prefer specific claims over vague ones. Include dates, standard names, and scope
   exactly as the passage states them.
4. If the evidence does not answer the question, return an empty findings list.
   An empty list is a correct and useful answer. Do not pad.
5. Do not infer. If a passage says a company "underwent an audit", that is not the
   same as "holds SOC 2 Type II certification". State only what is written.

Return ONLY JSON in this shape:
{"findings": [{"claim": "<one sentence>", "evidence_ids": ["<id>", ...]}]}"""


def _render_pool(pool: list[Evidence]) -> str:
    return "\n\n".join(e.for_prompt() for e in pool)


def enforce_citation_invariant(
    findings: list[Finding], pool: list[Evidence]
) -> list[Finding]:
    """A finding is `supported` only if it cites at least one evidence id that exists.

    Unknown ids are dropped rather than trusted: a hallucinated citation is worse
    than no citation, because it looks checked.
    """
    valid_ids = {e.id for e in pool}
    for f in findings:
        resolved = [eid for eid in f.evidence_ids if eid in valid_ids]
        dropped = [eid for eid in f.evidence_ids if eid not in valid_ids]
        f.evidence_ids = resolved
        if not resolved:
            f.status = Status.UNVERIFIED
            f.demoted_reason = (
                f"no resolvable evidence (model cited unknown ids: {dropped})"
                if dropped
                else "no evidence cited"
            )
        else:
            f.status = Status.SUPPORTED
            if dropped:
                f.demoted_reason = f"dropped unknown evidence ids: {dropped}"
    return findings


def extract_findings(
    llm: LLM,
    model: str,
    dimension: Dimension,
    pool: list[Evidence],
    subject: str,
) -> list[Finding]:
    """Ask the writer for findings over the evidence pool, then enforce the invariant.

    The subject name IS passed -- a correction from the first version of this design,
    which withheld it to discourage answering from memory. Withholding it did not stop
    memory-answering (the citation invariant and the verifier do that); it only left
    the writer unable to tell whose facts it was reading, and it duly reported another
    company's subprocessor list as if it were the subject's. Groundedness is not
    relevance, and they need separate mechanisms.
    """
    if not pool:
        return []

    user = (
        f"SUBJECT: {subject}\n\n"
        f"QUESTION: {dimension.question}\n\n"
        f"EVIDENCE PASSAGES:\n{_render_pool(pool)}\n\n"
        "Return JSON only."
    )

    try:
        data = llm.chat_json(
            model=model,
            system=WRITER_SYSTEM,
            user=user,
            span_name=f"findings.extract.{dimension.key}",
            attributes={
                "diligence.dimension": dimension.key,
                "diligence.evidence_count": len(pool),
            },
        )
    except Exception:
        return []

    raw = data.get("findings", []) if isinstance(data, dict) else []
    findings = [
        Finding(
            dimension=dimension.key,
            claim=str(item.get("claim", "")).strip(),
            status=Status.UNVERIFIED,
            evidence_ids=[str(x) for x in (item.get("evidence_ids") or [])],
        )
        for item in raw
        if isinstance(item, dict) and str(item.get("claim", "")).strip()
    ]

    return enforce_citation_invariant(findings, pool)
