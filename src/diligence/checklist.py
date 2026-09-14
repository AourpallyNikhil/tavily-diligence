"""The diligence checklist.

Deliberately a fixed, hand-written structure rather than an LLM-generated plan.
Real vendor security diligence *is* a checklist, and a deterministic plan is what
makes evaluation reproducible: an LLM-invented plan varies run to run and destroys
comparability between the baseline and this agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dimension:
    key: str
    title: str
    # Question put to the writer model, scoped to this dimension only.
    question: str
    # Query templates; {vendor} is substituted. The first is run steered to the
    # vendor's own domain (primary-source attempt), the rest run open.
    queries: tuple[str, ...]
    # Terms used by the deterministic passage selector to score chunks of page
    # text for relevance. Lowercased substring matching -- see evidence.py.
    keywords: tuple[str, ...] = field(default=())


CERTIFICATIONS = Dimension(
    key="certifications",
    title="Certifications & attestations",
    question=(
        "Which third-party security certifications or attestations does this vendor hold "
        "(for example SOC 2 Type II, ISO/IEC 27001, FedRAMP)?"
    ),
    queries=(
        "{vendor} SOC 2 Type II ISO 27001 certification compliance trust center",
        "{vendor} SOC 2 Type II attestation report",
        "{vendor} ISO 27001 certified security compliance",
    ),
    keywords=(
        "soc 2", "soc2", "soc ii", "type ii", "type 2",
        "iso 27001", "iso/iec 27001", "iso27001",
        "fedramp", "attestation", "certification", "certified",
        "audit report", "trust center", "trust centre", "compliance",
        "pci dss", "hipaa", "csa star",
    ),
)

BREACH_HISTORY = Dimension(
    key="breach_history",
    title="Breach & incident history",
    question=(
        "Has this vendor disclosed any security breach, compromise, or material security "
        "incident? Include the date and what was affected."
    ),
    queries=(
        "{vendor} security incident disclosure report",
        "{vendor} data breach incident unauthorized access",
        "{vendor} security incident postmortem customer data",
    ),
    keywords=(
        "breach", "incident", "compromise", "compromised", "unauthorized access",
        "unauthorised access", "attacker", "threat actor", "exfiltrat",
        "exposed", "disclosure", "cve-", "vulnerability", "ransomware",
        "phishing", "intrusion", "postmortem", "post-mortem",
    ),
)

SUBPROCESSORS = Dimension(
    key="subprocessors",
    title="Subprocessors & data residency",
    question=(
        "Which subprocessors does this vendor use, and in which regions or jurisdictions "
        "is customer data stored or processed?"
    ),
    queries=(
        "{vendor} subprocessors list data residency DPA",
        "{vendor} sub-processors GDPR data processing addendum",
        "{vendor} data residency region where is data stored",
    ),
    keywords=(
        "subprocessor", "sub-processor", "sub processor",
        "data residency", "data location", "data center", "data centre",
        "region", "jurisdiction", "gdpr", "dpa", "data processing addendum",
        "standard contractual clauses", "scc", "transfer", "hosted in",
        "eu", "us", "processor",
    ),
)

CHECKLIST: tuple[Dimension, ...] = (CERTIFICATIONS, BREACH_HISTORY, SUBPROCESSORS)

BY_KEY = {d.key: d for d in CHECKLIST}
