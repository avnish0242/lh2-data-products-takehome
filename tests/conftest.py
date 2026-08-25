"""Shared fixtures for the test suite.

`sample_*` fixtures load the actual sample_data/ fixtures the take-home ships with — that data
*is* the test data; there's no separate synthetic corpus for the integration-level tests. Several
unit tests below still construct small synthetic inputs directly where isolating one behavior is
clearer than reasoning about the full sample.

`er_mapping` and `deid_result` run the real pipeline stages once per test session, entirely
in-memory (`output_dir=None`), so tests never write into or depend on the checked-in output/
directory.
"""
import pytest

from lh2_pipeline import assemble_decision_unit, deidentify, paths, resolve_entities


@pytest.fixture(scope="session")
def sample_hr():
    return resolve_entities.load_hr()


@pytest.fixture(scope="session")
def sample_slack():
    return resolve_entities.load_json(paths.SLACK_JSON)


@pytest.fixture(scope="session")
def sample_jira():
    return resolve_entities.load_json(paths.JIRA_JSON)


@pytest.fixture(scope="session")
def sample_github():
    return resolve_entities.load_json(paths.GITHUB_JSON)


@pytest.fixture(scope="session")
def er_mapping(sample_hr, sample_slack, sample_jira, sample_github):
    return resolve_entities.run(sample_hr, sample_slack, sample_jira, sample_github, output_dir=None)


@pytest.fixture(scope="session")
def deid_result(er_mapping, sample_slack, sample_jira, sample_github):
    return deidentify.run(er_mapping, sample_slack, sample_jira, sample_github, output_dir=None)


@pytest.fixture(scope="session")
def decision_unit_and_audit(deid_result):
    return assemble_decision_unit.build_decision_unit(
        deid_result["slack"], deid_result["jira"], deid_result["github"]
    )


def surrogate_for(er_mapping, *, type_, value):
    """Test helper: find the surrogate_id of whichever person has an identifier of the given
    type+value (exact match). Raises if zero or more than one match, since tests use this to
    pin down a specific person via an unambiguous identifier."""
    hits = [
        p["surrogate_id"]
        for p in er_mapping["people"]
        for ident in p["identifiers"]
        if ident["type"] == type_ and ident["value"] == value
    ]
    assert len(hits) == 1, f"expected exactly one match for {type_}={value!r}, got {hits}"
    return hits[0]


def display_name_for(er_mapping, surrogate_id):
    return next(p["display_name_for_review_only"] for p in er_mapping["people"]
                if p["surrogate_id"] == surrogate_id)
