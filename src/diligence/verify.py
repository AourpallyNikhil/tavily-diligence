"""The verification gate.

Every supported finding is re-checked, claim against cited passage, by a second
model in a different role. The check is textual entailment and nothing else:

    Does THIS PASSAGE state THIS CLAIM?

The verifier is never shown the vendor name, the question, or any other evidence.
It cannot consult what it knows, because it has not been told what the subject is.
This is what stops the evaluation from degenerating into measuring agreement
between two instances of the same model's priors -- the failure mode any reviewer
should look for first in a setup like this.

A claim whose passages do not entail it is demoted to `unverified` in code. The
verifier's opinion is not advisory.
"""

from __future__ import annotations

from .evidence import Evidence
from .findings import Finding, Status
from .llm import LLM

VERIFIER_SYSTEM = """You are a strict textual entailment checker.

You are given one PASSAGE and one CLAIM. Decide whether the passage, on its own,
states the claim.

- "entailed": the passage explicitly states the claim, including any dates, scope,
  and qualifiers in the claim.
- "partial": the passage supports part of the claim but not all of it, or supports
  it more weakly or more vaguely than the claim asserts.
- "not_entailed": the passage does not state the claim, or contradicts it.

You have no knowledge of the world. If the passage does not say it, it is not
entailed, no matter how plausible the claim sounds.

Return ONLY JSON: {"verdict": "entailed|partial|not_entailed", "reason": "<short>"}"""

# Only full entailment keeps a claim supported. "partial" is a demotion: in
# diligence, a claim that overstates its source is the specific failure we care
# about -- "underwent an audit" becoming "holds SOC 2 Type II".
PASSING = {"entailed"}


def verify_findings(
    llm: LLM,
    model: str,
    findings: list[Finding],
    pool: list[Evidence],
) -> list[Finding]:
    by_id = {e.id: e for e in pool}

    for f in findings:
        if f.status is not Status.SUPPORTED or not f.evidence_ids:
            continue

        passed = False
        for eid in f.evidence_ids:
            ev = by_id.get(eid)
            if ev is None:
                f.verdicts[eid] = "missing"
                continue

            user = f"PASSAGE:\n{ev.passage}\n\nCLAIM:\n{f.claim}\n\nReturn JSON only."
            try:
                data = llm.chat_json(
                    model=model,
                    system=VERIFIER_SYSTEM,
                    user=user,
                    span_name="verify.entailment",
                    max_tokens=300,
                    attributes={
                        "diligence.evidence_id": eid,
                        "diligence.evidence_tier": ev.tier,
                        "diligence.dimension": f.dimension,
                    },
                )
                verdict = str(data.get("verdict", "not_entailed")).strip().lower()
            except Exception as exc:
                # A verifier that fails to answer must not silently pass a claim.
                verdict = "error"
                f.demoted_reason = f"verifier error: {str(exc)[:120]}"

            f.verdicts[eid] = verdict
            if verdict in PASSING:
                passed = True

        if not passed:
            f.status = Status.UNVERIFIED
            if not f.demoted_reason:
                f.demoted_reason = (
                    f"cited evidence did not entail the claim ({f.verdicts})"
                )

    return findings
