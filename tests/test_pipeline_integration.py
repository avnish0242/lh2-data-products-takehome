"""End-to-end pipeline test: resolve -> de-identify -> assemble, writing only into pytest's
tmp_path -- this never touches the checked-in output/ deliverable. Confirms the three modules are
actually wired together correctly (each stage's file-writing/loading contract, not just its
in-memory logic, which the other test files already cover)."""
import json

from lh2_pipeline import assemble_decision_unit, deidentify, resolve_entities


def test_full_pipeline_writes_expected_files(tmp_path, sample_hr, sample_slack, sample_jira, sample_github):
    er_dir = tmp_path / "er"
    deid_dir = tmp_path / "deidentified"
    du_dir = tmp_path / "decision_unit"

    mapping = resolve_entities.run(sample_hr, sample_slack, sample_jira, sample_github, output_dir=er_dir)
    assert (er_dir / "er_mapping.json").exists()
    assert json.loads((er_dir / "er_mapping.json").read_text()) == mapping

    deid = deidentify.run(mapping, sample_slack, sample_jira, sample_github, output_dir=deid_dir)
    for name in ("slack", "jira", "github"):
        assert (deid_dir / f"{name}.json").exists()
        assert json.loads((deid_dir / f"{name}.json").read_text()) == deid[name]

    decision_unit, audit = assemble_decision_unit.run(
        deid["slack"], deid["jira"], deid["github"], output_dir=du_dir
    )
    assert (du_dir / "decision_unit_pay123.json").exists()
    assert (du_dir / "decision_unit_pay123.internal_audit.json").exists()
    assert decision_unit["timeline"], "decision unit should not be empty for PAY-123"
    assert audit["excluded_events"], "the private Globex message should show up as excluded"


def test_pipeline_is_deterministic_across_two_independent_runs(
    tmp_path, sample_hr, sample_slack, sample_jira, sample_github
):
    def run_once(out_dir):
        mapping = resolve_entities.run(sample_hr, sample_slack, sample_jira, sample_github, output_dir=out_dir / "er")
        deid = deidentify.run(mapping, sample_slack, sample_jira, sample_github, output_dir=out_dir / "deid")
        return assemble_decision_unit.run(deid["slack"], deid["jira"], deid["github"], output_dir=out_dir / "du")

    result_a = run_once(tmp_path / "run_a")
    result_b = run_once(tmp_path / "run_b")
    assert result_a == result_b
