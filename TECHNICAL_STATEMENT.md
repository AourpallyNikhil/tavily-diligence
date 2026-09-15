# Technical statement

## What I built and why

A vendor security due-diligence agent that will not assert what it cannot cite.

The starter agent is a competent general research assistant. Its groundedness
story is one line of prompt — *"include source URLs when available"* — and that
line is the entire mechanism. Nothing requires it to search before answering;
nothing binds a claim to a source; nothing detects when retrieval missed the fact
and the model answered anyway. For open-ended research that is an acceptable
trade. For a report someone forwards to their security team, it is not.

I picked vendor security diligence deliberately, and not because it demos well.
It has a property most research tasks lack: **non-disclosure is normal.** No
vendor publishes its penetration test dates or its key rotation interval. That
makes "I cannot establish this" a *correct* answer with a checkable label, which
turns abstention into a measurable behaviour instead of a hedge. Most retrieval
evaluations can only ask *did it hallucinate*. Choosing this domain lets the
evaluation also ask *did it correctly refuse* — and that second question is where
almost all the enterprise value sits.

## The gaps I was actually fixing

I read the starter closely before designing anything. Seven groundedness defects,
ranked:

1. **Nothing requires a search.** `create_agent` hands the model a tool and lets
   it choose. A question it "knows" gets answered from weights, and the output is
   indistinguishable from a sourced one.
2. **Citation is a request.** No schema, no post-check, no failure path.
3. **No claim→source binding.** URLs arrive as a trailing bibliography; a reader
   cannot check the one fact they care about.
4. **Grounding on truncated snippets.** `TavilySearch()` takes every default.
5. **No recency or authority control.** A vendor's own filing and an SEO listicle
   carry equal weight.
6. **Display truncation mistaken for context management.** `truncate()` shapes the
   terminal; the full JSON still enters message history.
7. **One pass, no abstention path.** If retrieval missed it, the model answers anyway.

Underneath all seven: no observability, so none of it is measurable.

## Approach

Three mechanisms, each enforced in code rather than requested in a prompt.

**Citation as an invariant.** `enforce_citation_invariant` runs *after* the model
speaks and forces any finding without a resolvable evidence id to `unverified`.
Hallucinated ids are dropped rather than trusted — a fake citation is worse than
none, because it looks checked. This function cannot be prompted away, and it is
unit-tested.

**A verification gate that cannot consult its own memory.** Every supported claim
is re-checked by a second model asked exactly one question: *does this passage
state this claim?* The verifier is never told the vendor, the question, or any
other evidence. It has nothing to fall back on but the text in front of it. This
is the specific defence against the obvious objection to LLM-judged grounding —
that you end up measuring agreement between two instances of the same model's
priors. `partial` is a failure, not a pass: "underwent an audit" becoming "holds
SOC 2 Type II" is exactly the overstatement the gate exists to catch.

**Retrieve wide, then select.** This one came out of measurement, not planning —
see below.

**Authority as a role.** Each dimension declares which source roles are
authoritative for it, and every claim records whether it rests on one. Not a
demotion — a separate axis, because a claim can be perfectly entailed by a source
that had no business being the source for it.

Control flow is a fixed pipeline, not an agent loop. That is a deliberate
inversion of the starter. Autonomy is right for open-ended research and wrong for
a compliance artefact: a report that takes a different path every run cannot be
audited, diffed, or evaluated against a fixed question set.

## Four things I got wrong

Both are in the git history and the session log. They are the most useful part of
this submission.

**I designed the wrong fix for snippet truncation.** My design asserted that
`search_depth="advanced"` was the remedy. I measured it before writing code
against it: basic averaged 1,173 chars, advanced 1,279 — and advanced had *higher*
variance. The real lever was `include_raw_content`, which took the mean to 17,506
chars (13.7×). But that immediately created the opposite problem: five results
across three dimensions at that size is ~65k tokens of context, which recreates
gap #6 from the other direction. The resolution — fetch full page text, then
select from it by deterministic keyword scoring — is the context-engineering
contribution here, and it exists because a measurement contradicted my design.

**I over-engineered the evidence boundary and broke relevance.** My first version
deliberately withheld the vendor name from the writer, reasoning that a model
which is never told the subject cannot answer from memory about it. The first real
run produced this:

> ✓ Datadog is used as a subprocessor for monitoring by **Hex Technologies**.
> ✓ For **Hex Technologies**, Datadog's corporate location is the USA.

Grounded, cited, entailed by their passages — and answering about the wrong
company. Those are Hex's subprocessor disclosures, which name Datadog as *their*
vendor. Withholding the subject bought nothing (the citation invariant and the
gate are what stop memory-answering) and cost correctness. **Groundedness is not
relevance, and they need separate mechanisms.** The fix was to pass the subject
and add an explicit relevance rule, plus a second domain-steered retrieval pass.

**I modelled authority as a domain allowlist, and it inverted the ranking.** The
first version of `classify_tier` asked "is this domain trustworthy?" and answered
with 22 hardcoded entries. Running the finished agent by hand on Snowflake — a
vendor deliberately not in the eval set — exposed what that costs.

The 2024 Snowflake campaign is a contested-attribution case: customer accounts
without MFA were compromised via credential stuffing; Snowflake's own systems were
not breached. The agent surfaced both framings and marked both `supported`:

> ✓ *"Snowflake experienced a security incident … attackers compromised hundreds
> of organizations using Snowflake's cloud data platform"* — `nightfall.ai`
> ✓ *"Mandiant's investigation has not found any evidence to suggest that
> unauthorized access to Snowflake customer accounts stemmed from a breach of
> Snowflake's enterprise environment"* — `cloud.google.com`

Every claim was faithful to its source. The *set* was incoherent. And the reason
the wrong framing outranked the right one was structural, in three places:

1. `cloud.google.com` — where Mandiant publishes post-acquisition — collapsed to
   `google.com` under my eTLD+1 logic and fell through to `derivative`.
2. `cyber.gc.ca` and `ncsc.gov.uk` fell through too, because the `.gov` special
   case was US-only. Every non-US national CERT was invisible.
3. The intra-tier sort tiebreak was `-len(matched_keywords)`. Keyword density is
   what SEO content is built to maximise, so the ranker actively promoted content
   farms and buried the Canadian CERT advisory at position 21 of 21 for writing
   sparse factual prose.

None of that is fixable by prompting: the labels themselves were wrong, so even a
perfect "prefer authoritative sources" instruction would have faithfully applied a
mislabeled pool.

The fix is a reframe. **Authority is a relation between a source and a claim type,
not a property of a domain.** Sources now carry a *role*, and each dimension
declares which roles are authoritative for it — the vendor is the authority on its
own certifications and subprocessor list, and is explicitly *not* a neutral party
on attribution or root cause in its own breach. Provenance is recorded alongside
status rather than folded into it, because "is this claim supported by what it
cites" and "should that source have been the one to support it" are different
questions.

The honest limit: this still needs a prior about who is accountable. The
difference is that a per-dimension role policy is a small, defensible, auditable
prior, where a 22-domain allowlist was an undefended guess that silently
classified everything I had not thought of as junk.

**My provenance fix caused a regression, and the regression exposed an older
bug.** After the role change, all three breach-history items failed that had
passed before. My first diagnosis — that the new sort tiebreak had reordered the
pool — was wrong, and checking the data disproved it: the LastPass disclosure was
sitting at *position 1* of the evidence pool.

The actual cause was in passage selection. That page is 20,977 characters holding
four dated disclosures. The selector compressed it to 1,810 characters of the
most keyword-dense chunks, and the dates live in short headings that carry almost
no keywords. So the writer received the August 2022 incident text verbatim with
no date attached, and could not say when it happened.

Why it had passed before: the agent was getting the date from **Wikipedia**. Once
provenance steering pushed it onto the vendor's own disclosure, the primary
source turned out to be *worse evidence* — not because it says less, but because
my selector was discarding the part that mattered. Coverage fell and provenance
rose for the same reason. The regression did not introduce the bug; it revealed
one that a secondary source had been masking.

The fix makes selection date-aware on temporal dimensions: chunks carry their
section heading, dated chunks are boosted, and `breach_history` gets a larger
budget. Two of the three items recovered. The third, Okta's January 2022
statement, fails for an unrelated reason worth stating separately — it is never
retrieved at all. Zero 2022-dated Okta pages enter the pool, because the October
2023 support-system breach dominates every generic query. "Breach history" is
silently answering "most recent breach", and no amount of better selection fixes
a document that was never fetched.

## Evaluation

The central methodological decision: **dataset construction has to follow from the
metric, because precision can be judged from the output alone and recall cannot.**

Given a claim and its cited passage, deciding whether one supports the other needs
only the trace — cheap, no external research. But *"did it miss Okta's 2022
incident?"* is unanswerable from traces, because a fact the agent never surfaced
leaves nothing to annotate. In vendor diligence the missed breach is the scariest
failure, so recall cannot be left unmeasured. Hence a hybrid:

- **Hand-curated seed** (10 items) for coverage and abstention. Ground truth
  established by opening primary sources and transcribing them, which also keeps
  the comparison fair — the question set is agent-neutral, so neither system's
  behaviour shaped what it was asked. 7 of the 10 were then independently
  re-checked against their cited page by a human reviewer and all 7 held. The
  file distinguishes those (`human-signed-off`) from the 3 that were only
  confirmed during construction (`primary-source-confirmed`), because those are
  different claims and collapsing them would overstate the set's rigour.
- **Trace-annotated claims** for claim-level precision at a scale hand curation
  cannot reach.

I considered deriving the whole set from traces, which is the standard production
loop and cheaper per label. I rejected it for one disqualifying reason: a dataset
derived from the improved agent's own traces, used to compare improved against
baseline, is rigged in the improved agent's favour. That flaw alone would
discredit the headline table.

Metric definitions are borrowed and cited rather than invented — citation
precision from ALCE, faithfulness from RAGAS. Inventing private metrics would
have been easier and worth less.

**One number should be read sceptically, so I will flag it myself:** citation
precision for this pipeline is ~100% *by construction*. The gate demotes anything
its citation fails to entail, so whatever remains labelled supported necessarily
passed. That is not an achievement, it is a tautology. The informative numbers are
the gate's yield — how many claims it had to remove — and the baseline's
unenforced precision measured identically.

The baseline is deliberately charitable: it gets the same structured output shape
so that formatting is not the variable, and differs in retrieval (starter
defaults) and enforcement (none).

## What the evaluation actually showed

Five vendors, ten seed items, live APIs, run three times.

| metric | run 1 | run 2 | run 3 |
|---|---|---|---|
| coverage — agent | 71% (5/7) | 57% (4/7) | 57% (4/7) |
| coverage — baseline | 57% (4/7) | 43% (3/7) | 57% (4/7) |
| citation precision — baseline | 78% | 90% | 91% |
| provenance — agent / baseline | — | 96% / 75% | 95% / 72% |
| gate yield | 4% | 5% | 6% |

**The one real, stable result is provenance: +23 points.** 95-96% of this agent's
supported claims rest on a source whose role is authoritative for that claim
type, against 72-75% for the baseline. It is the metric the role model was built
to move, it held across two runs, and it is the only large repeatable gap.

**Coverage shows no difference.** 5, 4, 4 against 4, 3, 4. At n=7 one item is 14
points and both systems move by an item between runs. I make no coverage claim.

**Abstention showed no effect in any run** — 3/3 for both, three times. That was
the hypothesis I designed the project around. My negative items are too easy:
neither system comes near a pen-test date, so both abstain trivially. The fix is
near-miss negatives, and it is the first thing I would do next.

**I overstated citation precision and am correcting it.** After run 1 I wrote that
22% of baseline claims were unsupported by their own citation and called it "the
one solid result". Runs 2 and 3 put it at 10% and 9%. The 78% was an outlier. The
gap is real but about a third of what I first claimed — and the correction is the
point: a single run over live web retrieval is not evidence, and I would not have
known that without running it three times.

**Cost — two different claims, which I had been quoting as one.** The gate costs
~14x the LLM calls (one verifier call per claim per citation), and that is
inherent to enforcement. The ~10x wall-clock is mostly not: the pipeline is
entirely synchronous, so 15 searches and ~25 verifications per vendor run
sequentially. Parallelising both is a thread pool, not a redesign.

I am reporting two null results and one self-correction on my own headline
metrics, because the alternative is a submission whose numbers cannot be trusted.
The measurement was worth doing precisely because it disagreed with me three
separate times.

## Observability

OpenTelemetry with OpenInference semantic conventions over OTLP/HTTP, defaulting
to a local Arize Phoenix. Choosing the open standard over a vendor SDK is the
point: `OTEL_EXPORTER_OTLP_ENDPOINT` repoints the same spans at Langfuse,
LangSmith or Jaeger with no code change.

Tracing here is not a bonus-points bolt-on. The eval harness reads these spans
back to build its claim-level dataset, so instrumentation and evaluation are one
system rather than two. One report emits ~46 spans, with token counts and
per-source authority tiers as attributes.

## Business value

For a Tavily enterprise customer, the blocker on shipping a research agent is
rarely capability — it is auditability. Nobody forwards an LLM summary to their
security team without being able to check it. This system makes three things true
that were not true before:

- **Every assertion is checkable in one click**, bound to the passage that
  supports it and tagged with source authority (vendor-primary / authoritative
  third party / secondary).
- **The system says "I don't know."** For procurement that is the feature, not a
  limitation — a tool that invents a SOC 2 attestation is worse than no tool.
- **Changes are measurable.** A prompt tweak can be shown to help or hurt before
  it reaches a customer.

The cost is real and I have not hidden it, but it needs splitting: enforcement
costs roughly 19x the LLM calls, which is inherent, while the wall-clock gap is
mostly that the pipeline is synchronous and I did not parallelise the search and
verification fan-outs. For a report attached to a procurement decision the call
cost is the right trade; the latency is simply unfinished work.

## What I would do next

Embedding-based passage selection (keyword scoring is the cheap version); a
contradiction status distinct from `unverified`; expanding the seed set and
reporting confidence intervals rather than point estimates; caching retrieval so
re-runs are cheap enough to evaluate prompt changes continuously.

## On tooling

Built with Claude Code. The full session log is included as the build record. What
it shows is the honest version: two design errors caught by measurement rather
than by review, several assertions I made and then checked (the Cloudflare
subprocessor URL in the gold set was wrong on first attempt; a Twilio item was
dropped when no primary source could be found), and the numbers in this repository
all produced by runs that actually happened against live APIs.
