# tavily-diligence

A vendor security due-diligence agent that will not assert what it cannot cite.

Given a vendor, it works a fixed diligence checklist against the live web via
Tavily and returns a findings table in which every row is either bound to a
quoted source passage or explicitly marked `unverified`.

```bash
uv run diligence.py "Datadog" --domain datadoghq.com
```

```
Subprocessors & data residency
┏━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃     ┃ Finding                                   ┃ Sources                   ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ ✓   │ Datadog may appoint Subprocessors under   │ [vendor-primary]          │
│     │ the SCCs as set out in its Data           │ https://www.datadoghq.com…│
│     │ Processing Addendum.                      │                           │
│ ?   │ Datadog has an EU Region in Germany.      │ cited evidence did not    │
│     │                                           │ entail the claim          │
└─────┴───────────────────────────────────────────┴───────────────────────────┘
```

The second row is the point. The model wrote that claim; the evidence it cited
did not support it; it was demoted in code, not by persuasion.

## Why vendor diligence

Because in this domain **non-disclosure is normal.** Vendors do not publish
penetration test dates or internal control implementations. That makes "I cannot
establish this" a *correct* answer with a checkable label — which turns abstention
into something measurable rather than a hedge. Most retrieval evaluations can only
ask "did it hallucinate." This one can also ask "did it correctly refuse."

(In the event, my negative items were too easy to discriminate between the two
systems — see [Results](#results). The design admits the question; this seed set
was not sharp enough to answer it.)

## How it works

```
vendor
  │
  ▼  fixed 3-dimension checklist (not LLM-planned — reproducible)
  │
  ▼  Tavily: 2 domain-steered passes + 3 open passes per dimension
  │          include_raw_content, then deterministic passage selection
  │
  ▼  evidence pool — {id, url, role, passage, matched_keywords}
  │     role ∈ vendor-primary | regulator-cert | forensics |
  │            vuln-registry | press-of-record | derivative
  │
  ▼  writer emits claims, each citing evidence ids
  │     (given this dimension's source-authority policy)
  │
  ▼  ENFORCED: no resolvable citation ⇒ unverified          (findings.py)
  │
  ▼  ENFORCED: citation must entail the claim ⇒ else unverified  (verify.py)
  │
  ▼  RECORDED: is the cited role authoritative here?        (findings.py)
  │
  ▼  findings table
```

Three mechanisms carry the weight.

**1. Citation as an invariant, not an instruction.** The starter agent's entire
citation mechanism is a sentence in a prompt — *"include source URLs when
available."* That is a request. Here, `enforce_citation_invariant` runs after the
model has spoken and forces any finding without a resolvable evidence id to
`unverified`. Hallucinated ids are dropped rather than trusted, because a fake
citation is worse than none: it looks checked.

**2. A verification gate that cannot consult its own memory.** Every supported
claim is re-checked by a second model asked one question — *does this passage
state this claim?* The verifier is never told the vendor, the question, or any
other evidence, so it has nothing to fall back on but the text in front of it.
This is what stops the system from measuring agreement between two instances of
the same model's priors. `partial` is a failure, not a pass: "underwent an audit"
becoming "holds SOC 2 Type II" is precisely the overstatement this exists to catch.

**3. Authority as a role, not a domain allowlist.** The first version asked "is
this domain trustworthy?" and answered with a 22-entry list. A live Snowflake run
showed what that costs: Mandiant's forensic report on UNC5537 — the definitive
account of the 2024 campaign — was tagged `secondary`, because it publishes under
`cloud.google.com` and the eTLD+1 logic collapsed that to `google.com`. The
Canadian Centre for Cyber Security advisory was `secondary` too, because the
`.gov` special case was US-only. Both ranked *below* marketing blogs that got the
attribution wrong.

The fix is a reframe: **authority is a relation between a source and a claim
type, not a property of a domain.** Sources are classified into roles
(`vendor-primary`, `regulator-cert`, `forensics`, `vuln-registry`,
`press-of-record`, `derivative`) and each dimension declares which roles are
authoritative *for it*:

| dimension | authoritative | why |
|---|---|---|
| certifications | vendor-primary, regulator-cert | the vendor is the authority on its own attestations |
| subprocessors | vendor-primary | contractual fact; only the vendor can state it |
| breach history | regulator-cert, forensics, press-of-record, vendor-primary | the vendor is authoritative that an incident *happened* — not on attribution, root cause, or scope |

That last row is the whole point. A vendor is not a neutral party about its own
culpability, and one global notion of authority cannot express that.

Provenance is **recorded, not enforced**: `status` answers "is this claim
supported by what it cites," `provenance_ok` answers "should that source have
been the one to support it." Collapsing them would hide the distinction and
silently drop true claims that happen to be derivatively sourced.

**4. Retrieve wide, then select.** Measured on this workload: Tavily's `content`
field averages ~1.3k chars, `include_raw_content` ~17.5k (13.7×). The extra text
is where the supporting fact usually lives — but five results across three
dimensions at that size is ~65k tokens of context. So the agent fetches full page
text and then selects from it, by deterministic keyword scoring rather than
embeddings. Deterministic because it is reproducible across eval runs,
explainable (you can see which terms selected a passage), and free.

Selection is **date-aware on temporal dimensions**, which it was not originally.
A vendor's incident page is typically one long document holding several dated
disclosures — `Original post from August 25, 2022`, `Update as of December 22,
2022` — and those headings are short and keyword-poor, so keyword scoring threw
them away. The cost was measured: the selector pulled LastPass's August 2022
disclosure text *verbatim* while discarding its date, leaving the writer unable
to say when it happened. `breach_history` now declares `temporal=True`, chunks
carry their section heading, dated chunks are boosted, and the dimension gets a
larger char budget (3,200 vs 1,800) because one page can hold several incidents.

The heading is attached unconditionally rather than only when the body lacks a
date — the December section mentions "August of 2022" while referring *back* to
the earlier incident, so "body contains a date" would have kept the wrong one.

One trap here, found the hard way. The intra-tier sort tiebreak was originally
`-len(matched_keywords)` — and keyword density is *exactly* what SEO content is
optimised to maximise. On the Snowflake run it placed marketing blogs 10th–14th
of 21 and the Canadian CERT advisory **last**, because the CERT writes sparse
factual prose while the blogs repeat "breach" and "incident" constantly. The
ranker rewarded precisely the sources it should have discounted. Keyword overlap
is now capped at 3, and the final tiebreak is Tavily's own relevance rank — a
signal the source does not control.

## Results

Generated by `uv run eval/run_eval.py`; latest raw output in
[`eval/results/summary.md`](eval/results/summary.md), per-item verdicts in
`per_item.json`, claim-level labels in `claim_labels.json`.

Latest run — 5 vendors, 10 seed items, live APIs:

| metric | agent | baseline |
|---|---|---|
| coverage (positives, n=7) | 57% (4/7) | 57% (4/7) |
| abstention accuracy (negatives, n=3) | 100% (3/3) | 100% (3/3) |
| claims presented as supported | 111 | 53 |
| of those, entailed by their citation | 100% | 91% |
| **of those, on an authoritative source** | **95%** | **72%** |
| gate yield (claims removed) | 6% | n/a |
| elapsed / llm calls | 677s / 217 | 67s / 15 |

### Three runs, because one run means nothing here

The same harness was run three times against the live web. Runs 2 and 3 differ
only in the temporal-selection fix; the baseline code never changed.

| metric | run 1 | run 2 | run 3 |
|---|---|---|---|
| coverage — agent | 71% (5/7) | 57% (4/7) | 57% (4/7) |
| coverage — baseline | 57% (4/7) | 43% (3/7) | 57% (4/7) |
| citation precision — baseline | 78% | 90% | 91% |
| provenance — agent | — | 96% | 95% |
| provenance — baseline | — | 75% | 72% |
| gate yield | 4% | 5% | 6% |

### What these numbers actually support

**One result is real and stable: provenance, +23 points.** 95-96% of this agent's
supported claims rest on a source whose role is authoritative for that claim
type, against 72-75% for the prompt-only baseline. It held across two runs, it is
the metric the role model was built to move, and it is the only measured gap that
is both large and repeatable.

**Coverage shows no difference.** Agent 5, 4, 4 of 7 across three runs; baseline
4, 3, 4. At n=7 a single item is 14 points, and both systems move by an item
between runs. There is no coverage claim to make here, and I am not making one.

**Abstention showed no effect, in every run.** 3/3 for both systems, all three
times. This was the hypothesis the project was designed around, and the negative
items are simply too easy — neither system comes near a pen-test date, so both
abstain trivially. That is a flaw in my seed design, not a property of either
system. Discriminating negatives have to be *near-misses*: facts adjacent to
something findable, where a system under pressure to answer reaches for the
neighbouring source and overstates it.

**I overstated citation precision earlier and am correcting it.** After run 1 I
reported that 22% of baseline claims were unsupported by their own citation and
called it "the one solid result". Runs 2 and 3 put that at 10% and 9%. The 78%
was an outlier; the stable value is ~90%. The gap is real but roughly a third of
what I first claimed.

**The gate fires rarely and consistently** — 4%, 5%, 6% of emitted claims. Its
value is as a guarantee, not a frequent corrector.

### Reading the precision number honestly

For this pipeline, citation precision is ~100% **by construction** — the gate
demotes anything its citation fails to entail, so whatever remains labelled
supported necessarily passed. That is a tautology, not an achievement. The
informative figures are the gate's yield and the baseline's unenforced ~90%.

### How it is measured

Evaluation is split by metric, because **precision can be judged from the output
alone and recall cannot.** Given a claim and its cited passage, deciding whether
one supports the other needs nothing but the trace. But "did it miss Okta's 2022
incident?" is unanswerable from traces -- a fact never surfaced leaves nothing to
annotate. So:

- **Hand-curated seed** ([`eval/gold.yaml`](eval/gold.yaml), 10 items) -- ground
  truth established by opening primary sources and transcribing them. Carries
  coverage and abstention, and keeps the comparison fair: the question set is
  agent-neutral, so neither system's behaviour shaped what it was asked.
  7 of 10 items were then independently re-checked against their cited page by a
  human reviewer and all 7 held; the file marks those `human-signed-off` and the
  remaining 3 `primary-source-confirmed`, because "the page was opened during
  construction" and "a second person verified it" are different claims.
- **Trace-annotated claims** (`eval/results/claim_labels.json`) -- every emitted
  claim with its cited passage, for claim-level precision at a scale hand
  curation cannot reach.

Metric definitions are borrowed, not invented: citation precision follows
[ALCE](https://arxiv.org/abs/2305.14627) (Gao et al., 2023), faithfulness follows
[RAGAS](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/).

## Observability

OpenTelemetry with OpenInference semantic conventions over OTLP/HTTP. The default
collector is a local [Arize Phoenix](https://github.com/Arize-ai/phoenix), but
nothing here is Phoenix-specific — point `OTEL_EXPORTER_OTLP_ENDPOINT` at
Langfuse, LangSmith, or Jaeger and the same spans land there. Instrument once
against the open standard, choose the backend later.

```bash
uv run phoenix serve          # http://localhost:6006
uv run diligence.py "Okta" --domain okta.com
```

One report produces ~46 spans: `diligence.report` → `diligence.dimension` ×3 →
`tavily.search` ×15, `findings.extract.*` ×3, `verify.entailment` ×24, with token
counts and per-source tier attributes.

Tracing is not decoration here — the eval harness reads these spans back to build
its claim-level dataset. Instrumentation and evaluation are one system.

## Setup

```bash
uv sync --extra dev
cp .env.example .env     # add TAVILY_API_KEY and OPENROUTER_API_KEY
uv run pytest            # 19 tests, no network required
```

Inference runs through an OpenAI-compatible endpoint. OpenRouter is the default;
Nebius Token Factory or any other compatible provider is an environment change:

```bash
LLM_BASE_URL=https://api.studio.nebius.com/v1
NEBIUS_API_KEY=...
WRITER_MODEL=...
VERIFIER_MODEL=...
```

Model tiering is deliberate. The verifier runs once per claim — the highest-volume
call in the system — but its job is narrow, so it runs on a small cheap model
while the writer handles synthesis.

## Tests

`tests/test_invariants.py` covers the three properties that are the product:
a finding with no resolvable citation is `unverified`; a hallucinated citation
does not count as a citation; a claim its own passage does not entail is demoted.
Including the case that matters most — a verifier that errors must not silently
pass a claim.

## Limitations

Stated because they are real, not to pre-empt criticism.

- **The seed set is 10 items** (7 positive, 3 negative). Everything above is
  underpowered; a one-item swing moves coverage by 14 points.
- **The negative items do not discriminate.** Both systems scored 3/3, so the
  metric this project was designed around could not distinguish them. Fixing this
  means near-miss negatives, and is the first thing I would do next.
- **The web mutates.** Every label is pinned to `accessed_on: 2026-09-14`. Labels
  should be re-checked before reuse.
- **The verifier is an LLM.** `eval/results/claim_labels.json` exists so its
  verdicts can be audited against human labels; agreement is reported rather than
  assumed.
- **Three dimensions is not a diligence checklist.** It is three dimensions.
- **Negative items rest on a judgement** that non-disclosure is structural in this
  industry rather than a failure of our search.
- **Passage selection is keyword-based.** Embedding-based selection is the obvious
  upgrade and was cut for scope.
- **Breach retrieval has no temporal spread.** Generic queries return a vendor's
  *latest* incident, not its history. Okta's January 2022 statement is never
  retrieved at all — zero 2022-dated Okta pages reach the evidence pool, because
  the October 2023 support-system breach dominates every query. An agent asked
  for "breach history" is answering "most recent breach". Year-scoped queries are
  the fix and are not implemented.
- **Coverage is still provenance-blind.** The judge scores claim substance, so a
  primary-source gold item can be satisfied by a Wikipedia citation. Provenance is
  now measured across all claims, but it is not yet asserted *per gold item* --
  `gold.yaml` records a `primary_url` for every item that the scorer never reads.
- **Roles are still a curated prior.** Smaller and more defensible than the
  allowlist it replaced, but `FORENSICS_PREFIXES` and `PRESS_HOSTS` are lists I
  wrote. A genuinely authoritative publisher I did not think of is still
  classified `derivative`. The signals that would generalise -- primary vs
  derivative publication, independence-weighted corroboration -- are not
  implemented.
- **Provenance is recorded, not enforced.** A weakly-sourced claim still renders
  as supported, marked `~`. Whether it should be demoted is a product decision I
  did not want to make silently.
- **Cost.** The agent is ~25× slower than the baseline per vendor. Enforcement is
  not free, and for this use case that is the right trade — but it is a trade.

## Layout

```
src/diligence/
  checklist.py   fixed diligence dimensions
  retrieve.py    Tavily, steered + tiered
  evidence.py    evidence records, authority tiering, passage selection
  findings.py    extraction + the citation invariant
  verify.py      the entailment gate
  pipeline.py    fixed control flow
  render.py      console output
  tracing.py     OpenTelemetry / OpenInference
eval/
  gold.yaml      hand-curated seed, with quoted primary sources
  baseline.py    starter-equivalent, for comparison
  run_eval.py    the harness
```
