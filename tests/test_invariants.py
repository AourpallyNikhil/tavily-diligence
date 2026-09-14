"""Tests for the guarantees that are enforced in code rather than in prompts.

These are the tests that matter. Everything else in this system is best-effort;
these three properties are the product:

  1. A finding with no resolvable citation is `unverified`.
  2. A hallucinated citation does not count as a citation.
  3. A claim its own cited passage does not entail is demoted.
"""

from __future__ import annotations

from diligence.evidence import Evidence, classify_tier, select_passages
from diligence.findings import Finding, Status, enforce_citation_invariant
from diligence.verify import verify_findings


def ev(eid: str, passage: str = "text", tier: int = 1) -> Evidence:
    return Evidence(
        id=eid,
        url=f"https://example.com/{eid}",
        title=eid,
        domain="example.com",
        tier=tier,
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


class TestTiering:
    def test_vendor_domain_is_tier_1(self):
        assert classify_tier("https://www.datadoghq.com/security/", "datadoghq.com") == 1

    def test_subdomain_of_vendor_is_tier_1(self):
        assert classify_tier("https://trust.datadoghq.com/", "datadoghq.com") == 1

    def test_registry_is_tier_2(self):
        assert classify_tier("https://nvd.nist.gov/vuln/detail/CVE-2024-1", "acme.com") == 2

    def test_gov_is_tier_2(self):
        assert classify_tier("https://www.cisa.gov/advisory", "acme.com") == 2

    def test_blog_is_tier_3(self):
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
