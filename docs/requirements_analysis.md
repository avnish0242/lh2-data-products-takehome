# Requirements Analysis — Entity Resolution, De-identification & Decision-Unit Assembly

*Internal working doc. Not the submission. Distilled into `submission_notes.md` (≤2 pages) for
delivery. Written against the LH2 take-home assignment and the `sample_data/` fixtures.*

## 1. Problem framing

LH2's core loop is:

```
Ingest → Normalize → Resolve identities → De-identify consistently → Assemble a sellable "decision" unit
```

Each stage compounds risk into the next: a bad identity match silently produces a bad
de-identification (wrong-but-confident surrogate, or worse, two real people merged into one), which
silently produces a bad decision unit (wrong attribution) sold as ground truth to an AI lab customer.
Because the *output* is training data for other people's models, correctness failures here don't
just embarrass one report — they get baked into every downstream model trained on the product, at a
scale where nobody re-checks the source. This reframes the whole task: **the system's job is not to
maximize resolved/de-identified coverage, it's to maximize the trustworthiness of what does get
labeled as resolved, and to be honest and structured about what doesn't.**

"Sellable" is therefore not just "PII-free." A record can be free of employee PII and still be unsafe
to sell — e.g. because it exposes a customer's confidential legal situation, or because a low-
confidence identity guess would misattribute an action to the wrong (real, sellable-identity-bearing)
person. Both classes of failure appear in the sample data (see §4).

## 2. Functional Requirements

### FR-1 — Multi-source ingest & normalization
Ingest four structurally distinct sources — CSV roster, Slack export, Jira export, GitHub export —
into a common internal event/mention model. Each source has its own identifier vocabulary (see FR-2)
and its own notion of "a person is referenced here": a Slack message's `user` field, a Jira issue's
`assignee_email` and `comments[].author`, a GitHub PR's `author` string and `commits[].author`, plus
free text inside message bodies, comment bodies, PR titles, and commit messages.

### FR-2 — Authoritative identity graph, rooted in HR
Treat `hr_directory.csv` as the authoritative (but incomplete) root of the identity graph. Each HR
row is a candidate person with up to five identifiers: `emp_id`, `work_email`, `slack_handle`,
`github_login`, `display_name`. Two HR rows are **never** auto-merged into one person just because
they resemble each other (see FR-4) — each row starts as its own node unless direct evidence links it
to another row.

### FR-3 — Deterministic cross-system matching, tiered by identifier strength
Match a raw mention to an identity node using **exact-match** identifiers only, in descending trust
order:
1. `emp_id` (only ever appears within HR itself in this dataset, but is the anchor for future sources)
2. `work_email` / any email field, matched exact-string, case-insensitive
3. Platform-native user ID (Slack `user` field matched to `slack_handle`)
4. `github_login`, matched exact-string against commit/PR author handles
5. Full `display_name`, exact string match, **only** when combined with corroborating context
   (see FR-5) — full-name-alone matches are inherently weaker than an ID/email match because names
   are not unique (see the two-Priya case, §4.3) and should be scored/labeled accordingly, not treated
   as tier-1.

Each match is recorded with its tier, its method, and a confidence score — never just a boolean.

### FR-4 — No similarity-based auto-merge across HR rows
Name/handle *resemblance* between two HR rows (shared surname, shared first initial, lookalike
handles like `akumar-acme` vs `akumar-ext`) is **not** sufficient to merge them into one person. A
merge requires either (a) a shared exact identifier, or (b) an explicit human-reviewed decision
recorded as such. Where resemblance exists without a shared identifier, the system must emit a
**candidate-link** annotation (flagged, low confidence, not applied) rather than silently merging or
silently ignoring the resemblance. This is the single highest-stakes rule in the whole system: the
cost of a false merge (attributing a contractor's action to an employee, or vice versa, in a sold
product) is categorically worse than the cost of a false split (same person appearing as two
surrogates) — a false split is a data-quality gap; a false merge is a wrong, confidently-stated fact
about a real person, sold as truth.

### FR-5 — Contextual / fuzzy resolution for bare mentions, with required corroboration
A bare first-name mention in free text (e.g., "cc Priya to review") cannot be resolved by name alone
when more than one identity shares that name. It may be resolved **with reduced confidence** when a
corroborating signal exists — same channel as a subsequent self-identified message from the correct
candidate, matching topic/issue key, team membership matching the channel's evident domain, temporal
proximity. Every contextually-resolved mention must carry the evidence used, distinctly labeled as
"contextual" so downstream consumers can filter by confidence.

### FR-6 — Explicit unresolved bucket, never a forced pick
Any mention that cannot clear a defined confidence floor (see NFR-2) is emitted as **unresolved**
with a reason code (e.g., `no_identifier_overlap`, `ambiguous_multi_candidate`,
`insufficient_context`), not assigned to the nearest-looking candidate. This is an explicit
assignment requirement ("do not force a pick") and is treated as a first-class output state, not an
error.

### FR-7 — Stable surrogate ID issuance
Every resolved person (one identity-graph node, per FR-2–FR-4) gets exactly one surrogate ID (e.g.
`PERSON_0001`), assigned once and stable across reruns/incremental ingest (see NFR-1). Surrogate IDs
are never reused for unresolved mentions.

### FR-8 — Consistent de-identification across structured fields and free text
Replace every person-identifying token — structured fields (`assignee_email`, comment/commit/PR
`author`, Slack `user`) and free-text occurrences of names/emails/handles/signatures embedded in
message bodies, comment bodies, PR titles, and commit messages — with that person's surrogate ID,
**the same surrogate everywhere they appear, across all four sources**. Unresolved mentions are
replaced with a generic non-attributing placeholder (e.g. `[UNRESOLVED_PERSON]`), never with a
guessed surrogate and never left as raw PII.

### FR-9 — Decision-unit assembly
Given one cross-system decision thread, assemble a single structured object: ordered timeline of
events, participants as surrogates (with role, e.g. reporter/assignee/reviewer/author), and citations
pointing back to the originating raw record (source system + record id) for every event. The object
must be independently auditable — every claim traces to a source pointer.

### FR-10 — Sell-safety policy filtering
Before inclusion in a sellable product, every candidate event/record is checked against a policy
layer, independent of identity resolution: `channel_visibility` (exclude `private`), `export_eligible`
flags where present, and content-level signals of third-party (customer/partner) confidential
information (e.g. explicit "keep this quiet" / dispute language). Failing any check excludes the
record from the sellable output — it is not merely de-identified, it is dropped, and the drop is
logged with a reason.

## 3. Non-Functional Requirements

- **NFR-1 — Determinism & idempotency.** Re-running the pipeline on unchanged input must produce
  identical surrogate IDs and identical resolution decisions. This is required both for correctness
  (a customer's product shouldn't silently reshuffle identities between deliveries) and for
  incremental ingest at scale (new data must extend, not recompute, the identity graph).
- **NFR-2 — Calibrated confidence, not booleans.** Every match — deterministic or contextual — carries
  a numeric/qualitative confidence and the evidence behind it. A minimum-confidence floor gates
  whether a match is "resolved" vs. "unresolved"; the floor is a tunable policy parameter, not a
  hardcoded constant, because different customers/products may have different risk tolerances.
- **NFR-3 — Auditability / provenance.** Every resolution and every redaction decision must be
  traceable: which rule fired, what evidence, what source record. This is required both to defend
  decisions in review (this take-home's own "live defense" framing is a preview of that need) and to
  support after-the-fact correction if a match is later proven wrong.
- **NFR-4 — Precision-biased merge policy.** As stated in FR-4: the system is tuned to under-merge
  rather than over-merge. This is a explicit, named trade-off (see §5), not an accident of the
  algorithm.
- **NFR-5 — Separation of the re-identification key from the product.** The identity crosswalk
  (surrogate → raw identifiers) is itself a sensitive artifact — it's a re-identification key for
  every other de-identified product built from it. It must be stored, access-controlled, and audited
  separately from the sellable de-identified data, never bundled into a customer deliverable.
- **NFR-6 — Non-trivial reversibility resistance.** The de-identified product itself must not leak
  identity via a weak scheme (e.g., `md5(email)` as the "surrogate," which is a dictionary-attackable
  pseudonym, not de-identification). Surrogate IDs must be arbitrary/sequential, not derived from PII
  by a reversible or guessable transform.
- **NFR-7 — Scalability.** See §6. Both the matching algorithm and the review workload must remain
  tractable as source count, record count, and person count grow by orders of magnitude.
- **NFR-8 — Extensibility.** Adding a new source type (email, docs) must mean adding a new
  normalizer + identifier extractor, not redesigning the identity graph or the matching tiers.
- **NFR-9 — Human-in-the-loop, not human-blocking.** Low-confidence/candidate-link/unresolved output
  must feed a review queue asynchronously; the pipeline must not block waiting on human review to
  produce *a* result — it produces a correctly-labeled partial result now, and can absorb a later
  human correction as an update to the identity graph (which, per NFR-1, must then propagate
  consistently to reprocessed downstream output).

## 4. Edge cases (grounded in `sample_data/`)

### 4.1 Missing identifiers in the authoritative source
Two HR rows ("Alex K.", "A. Kumar") have blank `emp_id`; one also has blank email, one has blank
Slack handle. The system must treat "missing" as "no evidence," never as "matches everything" or
"matches nothing" — a blank field simply removes that identifier tier from consideration for that row.

### 4.2 The false-merge trap: a three-way "Alex/Kumar" cluster with no shared strong identifier
The HR roster contains **three** distinct rows that plausibly refer to overlapping real-world
people, with **zero overlapping strong identifiers between any pair of them**:
- Row `10533` — Alex Kumar, employee, `alex.kumar@acme.com`, Slack `U201ALEX`, github `akumar-acme`.
- Row (blank emp_id) — "Alex K.", contractor, Slack `UNK_ALEXK` *(this is itself an authoritative
  HR-assigned identifier — see 4.3)*, no email, no github.
- Row (blank emp_id) — "A. Kumar", contractor, `akumar@gmail.com`, no Slack, github `akumar-ext`.

A commit (`c3d4`, "add regression test") is authored `akumar@gmail.com` — an **exact, deterministic**
match to the third row, not the first. A Jira comment ("linked PR #88") is authored
`alex.kumar@acme.com` — an exact match to the first row. Surface-level, all three read as "the same
Alex," moonlighting across a corporate account, a contractor Slack presence, and a personal
email/GitHub account; at the identifier level they are three HR rows sharing only weak, informal
resemblance (first name "Alex," surname/initial "Kumar"/"K.," a common `akumar-*` GitHub prefix
between rows 1 and 3). Per FR-4, these remain **three separate surrogates**, with flagged, unapplied
candidate-links between each pair, routed to human review. This is the single hardest judgment call
in the dataset — not a two-way ambiguity but a three-way one.

### 4.3 "Alex K." resolves to its own thin HR identity, not to a guess
It's tempting to treat the Slack message *"Alex K. here from the contractor side, happy to help if
needed"* (from Slack user `UNK_ALEXK`) as an unresolvable bare mention. It isn't: the HR roster
itself assigns `UNK_ALEXK` as that row's Slack handle, so the message resolves **deterministically**
(tier 3, exact identifier match) to that HR row's own surrogate — a real, distinct identity node,
just a thinly-attested one (only one identifier type on file: no email, no github, no emp_id). The
judgment call is not "resolve or leave this mention unresolved" — it's "resolve this mention to its
own thin node, and do not additionally fold that node into Alex Kumar or A. Kumar without stronger
evidence" (see 4.2). Conflating "thin identity" with "unresolved mention" would have been a mistake:
a sparsely-attested but authoritatively-sourced identity is not the same as an unresolvable one.

### 4.4 Homonym collision across teams: two Priyas
Priya Nair (payments, `U330PRIYA`) and Priya Shah (legal, `U441PRIYA`) are unambiguous individually,
but two Slack messages reference a bystander as bare "Priya" — one in the `payments` channel, one in
`legal-ops`. Each channel later contains a self-identified follow-up from the correct Priya. This is
resolved *contextually* (FR-5) with reduced confidence and explicit evidence recorded (channel +
topic + temporal proximity to the self-ID), not resolved as if it were a deterministic match, and not
left unresolved outright given the corroboration available — this is a deliberate middle case between
4.2/4.3 (too weak) and identifier matches (fully deterministic).

### 4.5 Personal/corporate identity boundary
The same underlying ambiguity as 4.2 shows up as a general class: a person's professional identity
(corporate email, Slack) and a tool-level identity (git commit email, which is user-configured and
routinely personal) are not guaranteed to be the same account, or even the same legal employment
relationship (employee vs. contractor), even when they resemble each other.

### 4.6 Identity embedded in free text, not just structured fields
A Slack message signs off "— Priya Shah" inside the body text; a GitHub PR author field is the
combined string `"Alice Chen <alice.chen@acme.com>"` rather than a clean single identifier. De-
identification that only touches known structured "author" fields would miss both. Free-text scanning
(name/email/handle patterns, informed by the resolved identity graph) is required, not optional.

### 4.7 Sensitivity beyond employee PII: third-party confidentiality
The private channel `legal-sensitive` (`channel_visibility: "private"`) contains a message tying the
PAY-123 bug directly to a named customer ("Globex") "mid-dispute," with an explicit instruction to
"keep the fix quiet." This is a sell-safety failure mode orthogonal to person de-identification: even
a perfectly de-identified version of this message is unsafe to sell, because it discloses a
customer's confidential legal posture. Policy filtering (FR-10) must catch this independent of who
said it.

### 4.8 Governance flags in source data
`jira.json`'s issue carries `export_eligible: true`. The system must treat this as an explicit,
respected governance signal — the presence of the field implies the possibility of `false` values in
other records that must gate inclusion, even though this sample only shows the positive case.

### 4.9 Heterogeneous identifier encodings per source
Plain email (Jira `assignee_email`), a combined `"Name <email>"` string (GitHub PR author), a bare
git-log short handle (`achen`) that happens to equal a GitHub login, an internal chat platform ID
(`U123ALICE`) that is meaningless outside Slack, and free-text name mentions — five different
encodings of "this is a person," each needing its own extraction/normalization step before matching
can even begin.

### 4.10 General edge-case classes not present in this sample but expected at scale
Deactivated/reassigned Slack handles or GitHub logins (handle reuse across different people over
time); multiple work emails per person (aliases, name changes, mergers/acquisitions); bot/service
accounts posing as commit or comment authors; a channel with `public` visibility whose *membership*
is still effectively restricted (visibility metadata lagging real access); a person with a common name
across three or more homonymous candidates rather than two; right-to-erasure requests requiring
retroactive removal of a person from a previously-sold product.

## 5. Solution options considered, with tradeoffs

| Decision | Option A | Option B | Choice & why |
|---|---|---|---|
| Matching strategy | Rule-based deterministic tiers + scored contextual matching | ML/embedding-based similarity matching over all fields | **A.** For this data volume and the stakes of a false merge, explainable rule tiers are auditable and directly defensible in the "live defense" sense; an embedding similarity score for "Alex Kumar" vs "A. Kumar" would likely score them as *similar*, which is exactly the wrong signal to weight heavily here without much more corroborating evidence than exists. B becomes worth revisiting at scale (§6) as a *candidate generator* feeding the same rule-based confirmation step, not as the decision-maker. |
| Identity data structure | Graph (nodes = identities, edges = identifier-source links + candidate-links) | Flat lookup table (identifier → surrogate) | **A.** A flat table can't represent "these two nodes resemble each other but are not merged" (the FR-4 candidate-link) or carry per-edge evidence/confidence; a graph can, and it generalizes cleanly to new source types (NFR-8). |
| Free-text PII scrubbing | Dictionary/regex seeded from the resolved identity graph (names, emails, handles as literal patterns) | General-purpose NER model | **A** for this dataset's scale and the requirement for zero false-merges in identity; the closed identity graph makes literal/pattern matching both sufficient and precisely auditable. **B is flagged as a scale requirement** (§6) — a closed dictionary approach doesn't scale to open-vocabulary free text at 1000x volume/variety, so production framing treats NER as a second-pass detector for *unresolved* PII (names not already in the identity graph), reconciled against the graph rather than replacing it. |
| Sell-safety filtering | Policy engine, allow-list style (a record must affirmatively pass all checks) | Denylist/redact-after-the-fact | **A.** An allow-list defaults to exclusion under uncertainty, matching the same precision-biased posture as FR-4; a denylist defaults to inclusion unless a known-bad pattern fires, which fails silently on novel sensitive content (exactly the Globex case — nothing about "Globex" is a known bad pattern, but the *language* around it — dispute, confidentiality request, private channel — is a policy signal an allow-list can require). |
| Ingest cadence | Batch, full reprocess per run | Incremental/streaming, append-only to the identity graph | Batch is used for this take-home (fixed, tiny input); **incremental is the stated production target** or the identity graph could not stay stable under NFR-1 at scale — see §6. |

## 6. Scale analysis — what breaks at 1000x

- **Fuzzy/contextual matching cost.** Naive pairwise comparison is O(n²) in the number of unresolved
  mentions; at 1000x person/message volume this is intractable. Requires blocking/indexing (e.g. by
  team, channel, time window) before any pairwise scoring, so only plausible candidate pairs are ever
  compared.
- **Review-queue backlog.** The volume of low-confidence/candidate-link/unresolved output scales with
  input volume; human review throughput does not. Without prioritization (by downstream impact — e.g.
  a mention that appears in many decision units matters more than an isolated one) the queue grows
  unboundedly and NFR-9's "non-blocking" design starts silently shipping large unresolved swaths.
- **Free-text scrubbing false-negative rate.** A closed dictionary/regex approach (chosen at this
  scale, §5) does not generalize to open-vocabulary mentions (nicknames, transliterations, typos,
  new hires not yet reflected in HR data at ingest time). At volume, the absolute count of missed PII
  grows even if the *rate* stays constant — this is the single biggest production risk and argues for
  a second-pass general NER detector reconciled against the identity graph, with any hit not already
  resolved treated as a hard-stop review item, not an auto-redaction.
- **Identity graph merge conflicts & provenance volume.** More sources and more identifiers per person
  means more edges and more candidate-links; the graph itself becomes a scaling concern (storage,
  query latency for "does this new mention match an existing node") and needs indexing by identifier,
  not full-graph scans.
- **Name-collision growth.** The two-Priya case is one collision in ~7 people (roughly 1-in-25 same
  first name pairs at this size); collision *rate* grows faster than linearly with population for a
  fixed name distribution (birthday-paradox shape), so contextual-resolution load (FR-5) grows
  disproportionately, not proportionately, with headcount.
- **Multi-tenant isolation.** At "1000x" LH2 is presumably serving many customer organizations, not
  one Acme; the identity crosswalk (NFR-5) must be partitioned per source-organization with no
  cross-tenant leakage — a bug that merges an identity across two unrelated customer datasets is a
  severe incident, not a data-quality bug.
- **Reprocessing cost of corrections.** A human-confirmed correction to the identity graph (e.g.
  resolving the Alex Kumar / A. Kumar candidate-link) must propagate to every previously-produced
  de-identified record and decision unit that involved that person, under NFR-1. At scale this is a
  targeted incremental re-emit (records touching that surrogate), not a full pipeline rerun — the
  architecture must support "recompute only what's affected," which requires tracking, per output
  record, which identity-graph nodes it depends on.

## 7. Assumptions & open questions (flagged, not guessed past)

- **Assumption:** Channel `channel_visibility` accurately reflects real access control at capture
  time (no lag between an actual permission change and the recorded visibility). Flagged as unverified
  in this sample — production would need to source visibility from the platform's live ACL, not a
  point-in-time export field.
- **Assumption:** `akumar@gmail.com` being used as the HR "work_email" field for the contractor row
  is intentional data modeling (contractors sometimes have no corporate email and HR records their
  personal contact instead), not a data-entry error conflating two people. Treated as intentional; if
  wrong, it doesn't change the resolution outcome (still two separate rows, still not merged) but
  would change how confidently we describe row 7 as "a contractor using a personal email."
- **Open question:** Is a Jira comment `author` field ("Priya Nair" as a plain string) sourced from
  Jira's own authenticated user directory (which would make it effectively as strong as an ID match)
  or just free text captured at export time? Treated conservatively as the latter (full-name string
  match, tier 5) since the fixture gives no schema signal either way — this is exactly the kind of
  assumption that should be confirmed with a real Jira API/export schema before productionizing.
- **Open question:** Should a contextually-resolved mention (§4.4, the "Priya" cases) be included in
  the sellable decision unit's structured fields, or only in an internal audit trail? This analysis
  treats confidence-labeled contextual resolutions as includable (with their confidence level carried
  through), but a more conservative posture would restrict decision-unit *participant* fields to
  deterministic matches only and drop contextual matches to a supplementary/internal-only field. This
  is called out explicitly in the judgment-calls note as a defensible alternative choice.
