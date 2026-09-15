"""Tests for the guarantees that are enforced in code rather than in prompts.

These are the tests that matter. Everything else in this system is best-effort;
these three properties are the product:

  1. A finding with no resolvable citation is `unverified`.
  2. A hallucinated citation does not count as a citation.
  3. A claim its own cited passage does not entail is demoted.
"""

from __future__ import annotations

from diligence.checklist import BREACH_HISTORY, SUBPROCESSORS
from diligence.evidence import Evidence, Role, classify_role, classify_tier, select_passages
from diligence.findings import (
    Finding,
    Status,
    annotate_provenance,
    enforce_citation_invariant,
)
from diligence.verify import verify_findings


def ev(eid: str, passage: str = "text", tier: int = 1, role: str = "vendor-primary") -> Evidence:
    return Evidence(
        id=eid,
        url=f"https://example.com/{eid}",
        title=eid,
        domain="example.com",
        tier=tier,
        role=role,
        passage=passage,
    )


def finding(claim: str, ids: list[str]) -> Finding:
    return Finding(
        dimension="certifications", claim=claim, status=Status.SUPPORTED, evidence_ids=ids
    )


class TestCitationInvariant:
    def test_no_citation_is_unverified(self):
        pool = [ev("cert-1")]
        out = enforce_citation_invariant([finding("Vendor holds SOC 2.", [])], pool)
        assert out[0].status is Status.UNVERIFIED
        assert out[0].demoted_reason == "no evidence cited"

    def test_hallucinated_citation_is_not_a_citation(self):
        pool = [ev("cert-1")]
        out = enforce_citation_invariant(
            [finding("Vendor holds ISO 27001.", ["cert-9"])], pool
        )
        assert out[0].status is Status.UNVERIFIED
        assert out[0].evidence_ids == []
        assert "cert-9" in out[0].demoted_reason

    def test_valid_citation_survives(self):
        pool = [ev("cert-1")]
        out = enforce_citation_invariant([finding("Vendor holds SOC 2.", ["cert-1"])], pool)
        assert out[0].status is Status.SUPPORTED
        assert out[0].evidence_ids == ["cert-1"]

    def test_mixed_citations_keep_only_real_ids(self):
        pool = [ev("cert-1")]
        out = enforce_citation_invariant(
            [finding("Vendor holds SOC 2.", ["cert-1", "cert-42"])], pool
        )
        assert out[0].status is Status.SUPPORTED
        assert out[0].evidence_ids == ["cert-1"]
        assert "cert-42" in out[0].demoted_reason


class _StubLLM:
    """Returns a scripted verdict per call, so the gate is tested without a network."""

    def __init__(self, verdicts: list[str]) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0

    def chat_json(self, **_kwargs):
        self.calls += 1
        return {"verdict": self._verdicts.pop(0), "reason": "stub"}


class TestVerificationGate:
    def test_not_entailed_is_demoted(self):
        pool = [ev("cert-1")]
        out = verify_findings(
            _StubLLM(["not_entailed"]), "m", [finding("Vendor holds SOC 2.", ["cert-1"])], pool
        )
        assert out[0].status is Status.UNVERIFIED

    def test_partial_is_demoted(self):
        # "underwent an audit" -> "holds SOC 2 Type II" is the exact overstatement
        # this gate exists to catch.
        pool = [ev("cert-1")]
        out = verify_findings(
            _StubLLM(["partial"]), "m", [finding("Vendor holds SOC 2 Type II.", ["cert-1"])], pool
        )
        assert out[0].status is Status.UNVERIFIED

    def test_entailed_survives(self):
        pool = [ev("cert-1")]
        out = verify_findings(
            _StubLLM(["entailed"]), "m", [finding("Vendor holds SOC 2.", ["cert-1"])], pool
        )
        assert out[0].status is Status.SUPPORTED

    def test_one_entailed_source_is_enough(self):
        pool = [ev("cert-1"), ev("cert-2")]
        out = verify_findings(
            _StubLLM(["not_entailed", "entailed"]),
            "m",
            [finding("Vendor holds SOC 2.", ["cert-1", "cert-2"])],
            pool,
        )
        assert out[0].status is Status.SUPPORTED

    def test_verifier_error_does_not_silently_pass(self):
        class Boom:
            def chat_json(self, **_kwargs):
                raise RuntimeError("upstream 500")

        pool = [ev("cert-1")]
        out = verify_findings(Boom(), "m", [finding("Vendor holds SOC 2.", ["cert-1"])], pool)
        assert out[0].status is Status.UNVERIFIED

    def test_already_unverified_is_not_reverified(self):
        pool = [ev("cert-1")]
        f = finding("Vendor holds SOC 2.", [])
        enforce_citation_invariant([f], pool)
        stub = _StubLLM([])
        verify_findings(stub, "m", [f], pool)
        assert stub.calls == 0


class TestRoles:
    def test_forensics_under_parent_company_domain(self):
        # Mandiant publishes under cloud.google.com since the acquisition. The
        # first version collapsed this to google.com and tagged it derivative.
        u = "https://cloud.google.com/blog/topics/threat-intelligence/unc5537-snowflake"
        assert classify_role(u, "snowflake.com") is Role.FORENSICS
        assert classify_tier(u, "snowflake.com") == 2

    def test_google_outside_threat_intel_path_is_not_forensics(self):
        assert classify_role("https://cloud.google.com/pricing", "acme.com") is Role.DERIVATIVE

    def test_non_us_cert_is_regulator(self):
        # .gov special-casing was US-only; these all fell through to derivative.
        for u in (
            "https://www.cyber.gc.ca/en/alerts-advisories/x",
            "https://www.ncsc.gov.uk/news/y",
            "https://www.cyber.gov.au/about-us/alerts/z",
        ):
            assert classify_role(u, "acme.com") is Role.REGULATOR, u

    def test_marketing_blog_is_derivative(self):
        assert classify_role("https://www.nightfall.ai/blog/x", "acme.com") is Role.DERIVATIVE

    def test_press_of_record(self):
        assert classify_role("https://krebsonsecurity.com/2024/06/x", "acme.com") is Role.PRESS


class TestProvenance:
    def test_derivative_source_fails_provenance_for_subprocessors(self):
        pool = [ev("subp-1", role="derivative", tier=3)]
        f = finding("Acme uses AWS as a subprocessor.", ["subp-1"])
        f.dimension = "subprocessors"
        out = annotate_provenance([f], pool, SUBPROCESSORS)
        assert out[0].provenance_ok is False

    def test_vendor_source_passes_provenance_for_subprocessors(self):
        pool = [ev("subp-1", role="vendor-primary", tier=1)]
        f = finding("Acme uses AWS as a subprocessor.", ["subp-1"])
        out = annotate_provenance([f], pool, SUBPROCESSORS)
        assert out[0].provenance_ok is True

    def test_forensics_passes_provenance_for_breach_history(self):
        pool = [ev("brea-1", role="forensics", tier=2)]
        f = finding("Root cause was compromised customer credentials.", ["brea-1"])
        out = annotate_provenance([f], pool, BREACH_HISTORY)
        assert out[0].provenance_ok is True

    def test_provenance_is_not_a_demotion(self):
        # A weakly-sourced claim stays `supported`. Status and provenance are
        # different questions and collapsing them hides the distinction.
        pool = [ev("subp-1", role="derivative", tier=3)]
        f = finding("Acme uses AWS.", ["subp-1"])
        out = annotate_provenance([f], pool, SUBPROCESSORS)
        assert out[0].status is Status.SUPPORTED
        assert out[0].provenance_ok is False


class TestTemporalSelection:
    """A vendor incident page is one document holding several dated disclosures.
    Keyword scoring drops the headings that carry the dates, which is how the
    August 2022 LastPass text reached the writer with no date attached."""

    PAGE = (
        "Original post from August 25, 2022\n"
        "We have determined that an unauthorized party gained access to portions of "
        "the LastPass development environment through a single compromised developer "
        "account and took portions of source code.\n"
        "Update as of Thursday, December 22, 2022\n"
        "An unknown threat actor accessed a cloud-based storage environment leveraging "
        "information obtained from the incident we previously disclosed in August of 2022.\n"
    )
    KW = ("unauthorized access", "threat actor", "incident", "compromised", "breach")

    def test_headings_carry_dates_into_the_passage(self):
        passage, _ = select_passages(self.PAGE, self.KW, temporal=True, max_chunks=5, max_chars=3200)
        assert "August 25, 2022" in passage
        assert "December 22, 2022" in passage

    def test_both_disclosures_survive_selection(self):
        passage, _ = select_passages(self.PAGE, self.KW, temporal=True, max_chunks=5, max_chars=3200)
        assert "development environment" in passage
        assert "cloud-based storage" in passage

    def test_heading_attached_even_when_body_has_another_date(self):
        # The December section mentions "August of 2022" while referring back to
        # the earlier incident. A body-has-a-date check would wrongly skip the
        # heading and mislabel the disclosure.
        passage, _ = select_passages(self.PAGE, self.KW, temporal=True, max_chunks=5, max_chars=3200)
        dec = passage[passage.index("cloud-based storage") - 200 : passage.index("cloud-based storage")]
        assert "December 22, 2022" in dec

    def test_non_temporal_selection_adds_no_headings(self):
        passage, _ = select_passages(self.PAGE, self.KW, temporal=False)
        assert "[Original post" not in passage

    def test_temporal_boost_prefers_dated_chunks(self):
        page = (
            "Our company values transparency and incident response readiness.\n"
            "March 3, 2021\n"
            "An incident affected one customer.\n"
        )
        passage, _ = select_passages(page, ("incident",), temporal=True, max_chunks=1, max_chars=900)
        assert "March 3, 2021" in passage


class TestTiering:
    def test_vendor_domain_is_tier_1(self):
        assert classify_tier("https://www.datadoghq.com/security/", "datadoghq.com") == 1

    def test_subdomain_of_vendor_is_tier_1(self):
        assert classify_tier("https://trust.datadoghq.com/", "datadoghq.com") == 1

    def test_registry_is_tier_2(self):
        assert classify_tier("https://nvd.nist.gov/vuln/detail/CVE-2024-1", "acme.com") == 2

    def test_derivative_blog_is_tier_3(self):
        assert classify_tier("https://www.nightfall.ai/blog/x", "acme.com") == 3

    def test_gov_is_tier_2(self):
        assert classify_tier("https://www.cisa.gov/advisory", "acme.com") == 2

    def test_unknown_domain_is_tier_3(self):
        assert classify_tier("https://someblog.example/post", "acme.com") == 3


class TestPassageSelection:
    def test_selects_chunk_containing_keywords(self):
        text = (
            "Our company was founded in 2011 and has offices worldwide. " * 20
            + "We maintain SOC 2 Type II and ISO 27001 certification, audited annually. "
            + "Our cafeteria serves lunch daily. " * 20
        )
        passage, matched = select_passages(text, ("soc 2", "iso 27001", "certification"))
        assert "SOC 2 Type II" in passage
        assert "soc 2" in matched
        assert len(passage) < len(text)

    def test_no_keyword_match_falls_back_to_prefix(self):
        passage, matched = select_passages("nothing relevant here at all", ("soc 2",))
        assert matched == []
        assert passage.startswith("nothing relevant")

    def test_empty_text_returns_empty(self):
        assert select_passages("", ("soc 2",)) == ("", [])

    def test_respects_char_budget(self):
        text = "SOC 2 compliance details. " * 500
        passage, _ = select_passages(text, ("soc 2",), max_chars=500)
        assert len(passage) <= 500
