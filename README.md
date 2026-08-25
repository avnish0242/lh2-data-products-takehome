# LH2 Data Products Take-Home

> **Start here → [`docs/submission_notes.md`](docs/submission_notes.md).** That's the actual
> ≤2-page deliverable the assignment asks for (design note + hardest judgment calls + what's not
> safe to sell + what breaks at 1000x). Everything else in `docs/` is the deeper internal analysis
> behind that note, kept separate so the required note stays tight instead of gold-plated.

## Read in this order

| # | File | What it is | Read it for |
|---|------|-----------|--------------|
| 1 | **[`docs/submission_notes.md`](docs/submission_notes.md)** | The ≤2-page deliverable note (design + judgment calls) | **The one thing to read if you only read one thing.** |
| 2 | [`docs/requirements_analysis.md`](docs/requirements_analysis.md) | Full internal analysis: FRs, NFRs, edge cases (grounded in the actual data quirks), solution options + tradeoffs, scale analysis, open questions | Why the design in #1 is what it is, and where I flagged uncertainty instead of guessing |
| 3 | [`docs/architecture.md`](docs/architecture.md) | Production-scale system design (diagrams, subsystems, data model, governance, **delta/incremental ingestion §7**), with a full PAY-123 walkthrough | How this generalizes past a 7-person, 4-file sample to LH2's real multi-tenant, continuously-updated pipeline |
| 4 | `output/er_mapping.json` | The ER mapping deliverable | Every surrogate, every raw identifier, match method + confidence + evidence, and the 3 open (never-merged) candidate links |
| 5 | `output/deidentified/*.json` | The de-identified data deliverable | Same person → same surrogate, everywhere, structured fields and free text alike |
| 6 | `output/decision_unit_pay123.json` | The assembled decision unit deliverable | The one cross-system PAY-123 thread, sellable as-is |
| 7 | `src/lh2_pipeline/` + `tests/` | The working code | One hard step (entity resolution) done for real, not stubbed — with a test suite backing the judgment calls, not just a demo run |

## Repo structure

```
founding_engineer_assignment.pdf   # the assignment brief (as given)
sample_data/                       # the raw input fixtures (as given) — hr_directory.csv, slack/jira/github.json
docs/
  submission_notes.md              # <- the deliverable note (start here)
  requirements_analysis.md         # internal: FRs / NFRs / edge cases / tradeoffs
  architecture.md                  # internal: production-scale system design
src/lh2_pipeline/                  # the pipeline, as an installable Python package
  paths.py                         # shared path constants
  resolve_entities.py              # stage 1: entity resolution -> the identity graph / ER mapping
  deidentify.py                    # stage 2: consistent de-identification
  assemble_decision_unit.py        # stage 3: decision-unit assembly + sell-safety policy
  pipeline.py                      # runs all three stages in order
tests/                             # pytest suite — one file per pipeline stage, one on delta ingestion, one integration test
output/                            # generated deliverables (see table above)
pyproject.toml                     # Poetry project + dependency management
```

## How to run

Requires [Poetry](https://python-poetry.org/) and Python ≥3.9. No runtime dependencies beyond the
standard library — Poetry here is for a reproducible environment and a clean test workflow, not
because the pipeline needs a dependency tree.

```bash
poetry install          # creates the venv, installs the one dev dependency (pytest)

poetry run run-pipeline # regenerates everything under output/ in one shot:
                         #   resolve-entities -> deidentify -> assemble-decision-unit

# or run each stage individually (e.g. to inspect er_mapping.json before de-identifying):
poetry run resolve-entities
poetry run deidentify
poetry run assemble-decision-unit
```

All three stages are deterministic — rerunning produces byte-identical output (verified in
`tests/test_pipeline_integration.py::test_pipeline_is_deterministic_across_two_independent_runs`).

## How to run the tests

```bash
poetry run pytest -v
```

44 tests across 5 files. Unit tests pin down the specific judgment calls documented in the notes
(e.g. `test_alex_kumar_and_a_kumar_contractor_are_different_surrogates`,
`test_candidate_link_requires_surname_or_login_anchor_not_bare_first_initial`,
`test_private_globex_message_excluded_from_sellable_unit`) so a future change can't silently flip
one without a test failing. `test_delta_ingestion.py` proves surrogate IDs survive HR-roster
reordering and mid-file inserts across separate persisted runs — a regression test for a real bug
this session found and fixed (see `docs/architecture.md` §7.1). One integration test runs the full
three-stage pipeline into a `tmp_path` — never the checked-in `output/` — to verify the stages are
actually wired together correctly, not just individually correct in memory.

## Output

- `output/er_mapping.json` — 7 people resolved (every HR row, including the thinly-attested
  Slack-only contractor row), **3 open-but-unmerged candidate links** (the three-way "Alex/Kumar"
  cluster — see the design note), **0 forced/unresolved mentions** in this sample.
- `output/identity_registry.json` *(gitignored, not in this repo)* — the persistent surrogate-ID
  store (docs/architecture.md §7.1): what makes IDs stable across separate runs as the HR roster
  changes, instead of being derived from row position. Regenerated locally on every
  `resolve-entities` run. Not a requested deliverable, and kept out of the repo on the same
  principle as NFR-5: crosswalk-adjacent files stay separate from what's published, even in a
  fictional-data take-home. `output/er_mapping.json` above is the one crosswalk-like file that
  *is* checked in, because the assignment explicitly asks for it as a deliverable.
- `output/deidentified/` — slack/jira/github with every person reference (structured fields *and*
  free text) replaced by a stable surrogate id, consistent across all three files.
- `output/decision_unit_pay123.json` — the one assembled, **sellable** decision unit.
- `output/decision_unit_pay123.internal_audit.json` — **internal only**: records what was excluded
  from the decision unit and why (the private-channel customer-dispute message), deliberately kept
  out of the sellable file itself — see `docs/architecture.md` §4.3 for why that split exists.
