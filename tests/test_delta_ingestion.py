"""Delta / incremental ingestion (docs/architecture.md Sec 7). These tests exist because the first
implementation assigned surrogate IDs by CSV row position, which is only stable for a single,
never-reordered file -- exactly the kind of thing that looks correct against one static fixture and
quietly breaks the moment the roster changes between runs. See resolve_entities.assign_surrogate_id
docstring for the fix."""
import copy

from lh2_pipeline import resolve_entities as re_mod

from conftest import surrogate_for


def _row(emp_id="", display_name="", work_email="", slack_handle="", github_login="", team=""):
    return {"emp_id": emp_id, "display_name": display_name, "work_email": work_email,
            "slack_handle": slack_handle, "github_login": github_login, "team": team}


# --------------------------------------------------------------------------------------
# End-to-end: persisted registry across two separate runs
# --------------------------------------------------------------------------------------

def test_reordering_and_inserting_a_row_does_not_reshuffle_existing_ids(
    tmp_path, sample_hr, sample_slack, sample_jira, sample_github
):
    registry_path = tmp_path / "identity_registry.json"

    run1 = re_mod.run(sample_hr, sample_slack, sample_jira, sample_github,
                       output_dir=tmp_path / "run1", registry_path=registry_path)
    alice_before = surrogate_for(run1, type_="work_email", value="alice.chen@acme.com")
    priya_nair_before = surrogate_for(run1, type_="work_email", value="priya.nair@acme.com")
    a_kumar_before = surrogate_for(run1, type_="work_email", value="akumar@gmail.com")

    # A later delta run: the roster file got a new hire inserted at the FRONT and the remaining
    # rows reversed -- exactly the kind of re-export that broke the old positional scheme.
    new_hire = _row(emp_id="99999", display_name="New Hire", work_email="new.hire@acme.com",
                     slack_handle="U999NEW", github_login="nhire", team="payments")
    reordered_hr = [new_hire, *reversed(sample_hr)]

    run2 = re_mod.run(reordered_hr, sample_slack, sample_jira, sample_github,
                       output_dir=tmp_path / "run2", registry_path=registry_path)

    assert surrogate_for(run2, type_="work_email", value="alice.chen@acme.com") == alice_before
    assert surrogate_for(run2, type_="work_email", value="priya.nair@acme.com") == priya_nair_before
    assert surrogate_for(run2, type_="work_email", value="akumar@gmail.com") == a_kumar_before

    new_hire_id = surrogate_for(run2, type_="work_email", value="new.hire@acme.com")
    assert new_hire_id not in {alice_before, priya_nair_before, a_kumar_before}
    assert len(run2["people"]) == 8  # 7 existing + 1 genuinely new


def test_three_run_delta_sequence_keeps_ids_stable_throughout(tmp_path):
    """Simulates three successive delta ingestions of a growing roster: each run only adds people,
    and every person's id, once minted, never changes again."""
    registry_path = tmp_path / "identity_registry.json"
    empty_source = ([], [], [])  # (slack, jira, github) -- irrelevant to this test

    hr_v1 = [_row(emp_id="1", display_name="Ann Lee", work_email="ann@acme.com", team="eng")]
    out_v1 = re_mod.run(hr_v1, *empty_source, output_dir=tmp_path / "v1", registry_path=registry_path)
    ann_id = surrogate_for(out_v1, type_="work_email", value="ann@acme.com")

    hr_v2 = [_row(emp_id="2", display_name="Bo Kim", work_email="bo@acme.com", team="eng"), *hr_v1]
    out_v2 = re_mod.run(hr_v2, *empty_source, output_dir=tmp_path / "v2", registry_path=registry_path)
    assert surrogate_for(out_v2, type_="work_email", value="ann@acme.com") == ann_id
    bo_id = surrogate_for(out_v2, type_="work_email", value="bo@acme.com")
    assert bo_id != ann_id

    hr_v3 = [*hr_v2, _row(emp_id="3", display_name="Cy Ora", work_email="cy@acme.com", team="eng")]
    out_v3 = re_mod.run(hr_v3, *empty_source, output_dir=tmp_path / "v3", registry_path=registry_path)
    assert surrogate_for(out_v3, type_="work_email", value="ann@acme.com") == ann_id
    assert surrogate_for(out_v3, type_="work_email", value="bo@acme.com") == bo_id
    assert len(out_v3["people"]) == 3


def test_row_enriched_with_new_field_keeps_same_id_via_older_identifier(tmp_path):
    """A contractor row that starts Slack-only and later gains a work_email must keep its original
    surrogate (matched via the still-present slack_handle), and the registry should pick up the new
    email so *future* runs can match on it too, even if the slack_handle later disappears."""
    registry_path = tmp_path / "identity_registry.json"
    empty_source = ([], [], [])

    v1 = [_row(display_name="Alex K.", slack_handle="UNK_ALEXK", team="contractors")]
    out1 = re_mod.run(v1, *empty_source, output_dir=tmp_path / "v1", registry_path=registry_path)
    alex_k_id = surrogate_for(out1, type_="slack_handle", value="UNK_ALEXK")

    v2 = [_row(display_name="Alex K.", slack_handle="UNK_ALEXK", work_email="alexk@acme.com",
                team="contractors")]
    out2 = re_mod.run(v2, *empty_source, output_dir=tmp_path / "v2", registry_path=registry_path)
    assert surrogate_for(out2, type_="slack_handle", value="UNK_ALEXK") == alex_k_id

    registry = re_mod.load_registry(registry_path)
    entry = next(p for p in registry["people"] if p["surrogate_id"] == alex_k_id)
    assert entry["work_email"] == "alexk@acme.com"  # enriched, not just matched-and-discarded


# --------------------------------------------------------------------------------------
# assign_surrogate_id() in isolation
# --------------------------------------------------------------------------------------

def test_fresh_registry_reproduces_original_sequential_behavior():
    """registry=None (or empty) must give exactly PERSON_0001, PERSON_0002, ... in file order --
    the original position-based behavior, for the common case of a single unchanging file."""
    rows = [_row(display_name="A", team="x"), _row(display_name="B", team="x"), _row(display_name="C", team="x")]
    nodes, _idx, _registry = re_mod.build_nodes(rows, registry=None)
    ids = [n.surrogate_id for n in nodes.values()]
    assert ids == ["PERSON_0001", "PERSON_0002", "PERSON_0003"]


def test_tier_priority_emp_id_beats_stale_slack_handle():
    """If a row's emp_id matches one registry entry but its slack_handle happens to match a
    *different* entry (e.g. a recycled handle), emp_id -- the strongest identifier -- wins."""
    registry = re_mod.empty_registry()
    registry["people"] = [
        {"surrogate_id": "PERSON_0001", "emp_id": "1", "work_email": None, "slack_handle": None,
         "github_login": None, "display_name": "Old Employee", "team": "eng"},
        {"surrogate_id": "PERSON_0002", "emp_id": None, "work_email": None, "slack_handle": "U-RECYCLED",
         "github_login": None, "display_name": "Someone Else", "team": "eng"},
    ]
    registry["next_seq"] = 3
    idx = re_mod.registry_indices(registry)
    row = _row(emp_id="1", display_name="Old Employee", slack_handle="U-RECYCLED", team="eng")
    sid, is_new, tier = re_mod.assign_surrogate_id(row, registry, idx)
    assert sid == "PERSON_0001"
    assert not is_new
    assert tier == "emp_id"


def test_name_team_fallback_is_labeled_weak_and_only_used_last():
    registry = re_mod.empty_registry()
    idx = re_mod.registry_indices(registry)
    row1 = _row(display_name="Sparse Person", team="contractors")  # no strong identifier at all
    sid1, is_new1, tier1 = re_mod.assign_surrogate_id(row1, registry, idx)
    assert is_new1 and tier1 == "new"

    row2 = _row(display_name="Sparse Person", team="contractors")  # reappears, still no strong id
    sid2, is_new2, tier2 = re_mod.assign_surrogate_id(row2, registry, idx)
    assert not is_new2
    assert sid2 == sid1
    assert "weak fallback" in tier2


def test_new_row_never_collides_with_an_existing_surrogate():
    registry = re_mod.empty_registry()
    idx = re_mod.registry_indices(registry)
    existing = _row(emp_id="1", display_name="Ann", work_email="ann@acme.com", team="eng")
    sid1, _, _ = re_mod.assign_surrogate_id(existing, registry, idx)

    genuinely_new = _row(emp_id="2", display_name="Bo", work_email="bo@acme.com", team="eng")
    sid2, is_new, _ = re_mod.assign_surrogate_id(genuinely_new, registry, idx)
    assert is_new
    assert sid2 != sid1


def test_registry_round_trips_through_disk(tmp_path):
    registry = re_mod.empty_registry()
    idx = re_mod.registry_indices(registry)
    re_mod.assign_surrogate_id(_row(emp_id="1", display_name="Ann", team="eng"), registry, idx)
    path = tmp_path / "registry.json"
    re_mod.save_registry(registry, path)
    reloaded = re_mod.load_registry(path)
    assert reloaded == registry
    assert copy.deepcopy(reloaded) == registry  # not aliasing the in-memory object
