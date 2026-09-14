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

Control flow is a fixed pipeline, not an agent loop. That is a deliberate
inversion of the starter. Autonomy is right for open-ended research and wrong for
a compliance artefact: a report that takes a different path every run cannot be
audited, diffed, or evaluated against a fixed question set.

## Two things I got wrong

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
  behaviour shaped what it was asked.
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
defaults) and enforcement (none). Results in `eval/results/summary.md`.

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

The cost is real and I have not hidden it: the agent is roughly 25× slower per
vendor than the prompt-only baseline. Enforcement is not free. For a report that
gets attached to a procurement decision, it is the right trade; for casual
research it would not be.

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
