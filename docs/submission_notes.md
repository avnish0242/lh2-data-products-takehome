# Notes (≤2 pages)

## Design note — resolution approach & where it could be wrong

**Approach.** HR is treated as the authoritative (but incomplete) identity root — one node per HR
row. Every other source's mentions are matched against it in a strict tier order, exact-match only:
`emp_id` → email → platform user ID (Slack) → `github_login` → full name (name-alone is weakest,
since names aren't unique). A match at any tier adds an identifier to that person's node and is
recorded with its tier, method, and confidence — never just a yes/no. Bare-name mentions with more
than one same-name candidate (two Priyas in this dataset) are resolved only with a corroborating
signal — same channel plus a same-channel self-identifying follow-up — and are labeled `contextual`,
lower confidence than an ID match. Anything that clears no bar — no shared identifier, no
corroborated context — is emitted as **unresolved** with a reason code, never guessed. Critically,
name/handle *resemblance* between two HR rows is never sufficient on its own to merge them; a merge
requires a shared exact identifier or an explicit human decision. Every resolved person gets one
stable surrogate ID (`PERSON_000x`), and de-identification substitutes it consistently into both
structured fields (assignee, author, comment author) and free text (message/comment/commit bodies),
so the same person reads the same everywhere.

**Where it could be wrong.** (1) The tier-5 contextual match is inference, not proof — "cc Priya" in
the payments channel is resolved to Priya Nair on channel/topic grounds, but nothing *guarantees*
the sender didn't mean someone else; a determined reviewer could reasonably contest it. (2) The
"full name in a Jira comment author field" tier assumes that string is free text, not an
authenticated-directory value — if Jira's real export schema ties `comments[].author` to a verified
account, it deserves a higher tier than I gave it; I don't have that schema, so I under-trust it by
design (flagged, not guessed past). (3) The system is deliberately biased to *under*-merge (a false
split costs a data-quality gap; a false merge costs a wrong fact sold as truth about a real person) —
this means some pairs a human would confidently merge (see judgment call #1 below) are left as two
surrogates plus a flagged, unapplied candidate-link. That's a conscious precision/recall trade, not
an oversight.

## Judgment calls, what's not safe to sell, and scale

**Hardest judgment call — the three-way "Alex/Kumar" cluster.** The roster has three rows that
plausibly overlap with no exact identifier shared between *any* pair: employee Alex Kumar
(`alex.kumar@acme.com`, github `akumar-acme`), contractor "A. Kumar" (`akumar@gmail.com`, github
`akumar-ext`), and contractor "Alex K." (Slack-only, `UNK_ALEXK`). A commit is authored by
`akumar@gmail.com` (deterministic match to the second row, not the employee); a Jira comment linking
that same PR is authored by the employee's exact email; "Alex K." self-introduces as a contractor in
Slack with no email or GitHub at all. Surface reading: possibly one human across a work account, a
personal account, and a contractor Slack presence. Identifier reading: three HR rows, pairwise zero
overlap. I kept all three as **separate surrogates** with three flagged-but-unapplied candidate-links,
because merging on name resemblance risks attributing one person's commit or comment to a different
real, named individual in a sold product — categorically worse than under-linking. I'd defend this
live but acknowledge a reasonable reviewer could argue the PR-linkage circumstantial evidence tips
the Alex Kumar / A. Kumar pair further than I did.

**Second hardest — recognizing "Alex K." as its own thin identity, not an unresolved mention.**
It's tempting to call the `UNK_ALEXK` Slack message unresolvable since it carries no email or GitHub
login. But the HR roster itself assigns `UNK_ALEXK` as that row's Slack handle, so the message
resolves deterministically to that row's own (sparse) identity — a real surrogate with only one
identifier on file, not a guess and not a blank. Conflating "thinly-attested" with "unresolved" would
have quietly dropped a real, distinct person from the ER mapping. The judgment call was keeping it as
its own node while still refusing to fold it into either Kumar (previous point).

**Third — bare "Priya" mentions.** Resolved contextually (channel + same-channel self-ID
follow-up) rather than left unresolved, because the corroboration is genuinely there — but this is a
judgment call about where the confidence floor sits, not a certainty, and I labeled it accordingly
rather than presenting it at the same confidence as an ID match.

**Not safe to sell.** The private `legal-sensitive` channel message tying PAY-123 to a named
customer ("Globex") "mid-dispute," with an explicit "keep the fix quiet" instruction, is excluded
from the decision unit on two independent grounds: channel visibility (`private`) and content-level
third-party confidentiality — a customer's legal exposure, not employee PII. This would stay excluded
even if perfectly de-identified, because the risk isn't about who said it, it's about what it reveals
about Acme's customer. I also excluded the raw identity crosswalk itself from anything sellable —
it's a re-identification key and belongs in a separately access-controlled store, never bundled with
the product.

**What breaks at 1000x scale.** (1) Contextual/resemblance matching is pairwise by nature — naive
comparison is O(n²) and needs blocking (by team/channel/time window) to stay tractable. (2) Human
review load for unresolved/candidate-link items scales with volume while review throughput doesn't —
needs impact-based prioritization, not FIFO, or the backlog silently grows into "shipped unresolved."
(3) A closed dictionary/regex free-text scrubber (right-sized for 7 people) has a false-negative rate
against open-vocabulary mentions (nicknames, typos, new hires) that stays roughly constant as a
*rate* but grows without bound in absolute missed-PII count — needs a second-pass NER detector
reconciled against the identity graph. (4) Same-name collisions grow faster than linearly with
headcount (birthday-paradox shape), so contextual-resolution load grows disproportionately. (5) A
human-confirmed correction to one person's identity must propagate to every previously-produced
output that involved them — at scale this requires tracking per-record identity dependencies so a
correction triggers a targeted re-emit, not a full reprocess.
