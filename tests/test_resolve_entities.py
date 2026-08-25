"""Entity resolution — the core deliverable. These tests exist to pin down the exact judgment
calls documented in docs/submission_notes.md so a future change can't silently flip them."""
from lh2_pipeline import resolve_entities as re_mod
from lh2_pipeline.resolve_entities import (
    Identifier, PersonNode, UnresolvedMention, candidate_links, is_weakly_attested,
    scan_free_text, token_resembles,
)

from conftest import display_name_for, surrogate_for


# --------------------------------------------------------------------------------------
# Whole-sample behavior (against the real sample_data/ fixtures)
# --------------------------------------------------------------------------------------

def test_seven_people_resolved(er_mapping):
    """Every HR row becomes exactly one surrogate, including the two incomplete contractor rows."""
    assert len(er_mapping["people"]) == 7


def test_no_mention_is_forced_or_dropped(er_mapping):
    """In this sample every raw mention ties to *some* HR row (see requirements_analysis.md
    Sec 4.3) -- the real ambiguity is at the merge level, not the mention level."""
    assert er_mapping["unresolved_mentions"] == []


def test_alex_kumar_and_a_kumar_contractor_are_different_surrogates(er_mapping):
    """The central false-merge trap: no shared strong identifier -> two separate people."""
    alex_kumar = surrogate_for(er_mapping, type_="work_email", value="alex.kumar@acme.com")
    a_kumar = surrogate_for(er_mapping, type_="work_email", value="akumar@gmail.com")
    assert alex_kumar != a_kumar


def test_personal_email_commit_attributed_to_contractor_not_employee(er_mapping):
    """Commit c3d4 is authored by akumar@gmail.com -- that must land on the contractor node
    (A. Kumar), never on the employee (Alex Kumar), purely because that's what the identifier
    evidence says, regardless of how similar the two names look."""
    contractor = surrogate_for(er_mapping, type_="work_email", value="akumar@gmail.com")
    commit_edges = [
        ident
        for p in er_mapping["people"]
        for ident in p["identifiers"]
        if ident["type"] == "commit_author_email" and ident["value"] == "akumar@gmail.com"
    ]
    assert len(commit_edges) == 1
    owning_person = next(p["surrogate_id"] for p in er_mapping["people"]
                          if commit_edges[0] in p["identifiers"])
    assert owning_person == contractor


def test_alex_k_contractor_resolves_to_its_own_node(er_mapping):
    """The Slack-only contractor row ('Alex K.', handle UNK_ALEXK) is a real, distinct, thinly-
    attested identity -- not merged into either Kumar, and not treated as unresolved."""
    alex_k = surrogate_for(er_mapping, type_="slack_handle", value="UNK_ALEXK")
    alex_kumar = surrogate_for(er_mapping, type_="work_email", value="alex.kumar@acme.com")
    a_kumar = surrogate_for(er_mapping, type_="work_email", value="akumar@gmail.com")
    assert len({alex_k, alex_kumar, a_kumar}) == 3
    assert display_name_for(er_mapping, alex_k) == "Alex K."


def test_three_way_alex_kumar_cluster_flagged_open_never_applied(er_mapping):
    """Exactly the three pairwise resemblances should surface, all open, none merged."""
    links = er_mapping["candidate_links"]
    assert len(links) == 3
    assert all(link["status"] == "open" for link in links)
    pairs = {frozenset((link["node_a_name"], link["node_b_name"])) for link in links}
    assert pairs == {
        frozenset({"Alex Kumar", "Alex K."}),
        frozenset({"Alex Kumar", "A. Kumar"}),
        frozenset({"Alex K.", "A. Kumar"}),
    }


def test_two_priyas_never_produce_a_candidate_link(er_mapping):
    """Both Priya rows are fully attested with independent, non-overlapping identifiers -- sharing
    a first name alone must not flag them for review (that would just be noise)."""
    names = {frozenset((l["node_a_name"], l["node_b_name"])) for l in er_mapping["candidate_links"]}
    assert frozenset({"Priya Nair", "Priya Shah"}) not in names


def test_bare_priya_resolved_contextually_per_channel(er_mapping):
    """The bare 'Priya' bystander mention in the payments channel and the one in legal-ops must
    resolve to two *different* people, disambiguated only by channel/team context."""
    priya_nair = surrogate_for(er_mapping, type_="work_email", value="priya.nair@acme.com")
    priya_shah = surrogate_for(er_mapping, type_="work_email", value="priya.shah@acme.com")

    def contextual_refs(surrogate_id):
        person = next(p for p in er_mapping["people"] if p["surrogate_id"] == surrogate_id)
        return {i["record_ref"] for i in person["identifiers"]
                if i["type"] == "bare_first_name_contextual"}

    nair_refs, shah_refs = contextual_refs(priya_nair), contextual_refs(priya_shah)
    assert nair_refs and shah_refs
    assert nair_refs.isdisjoint(shah_refs)
    assert all("C100" in ref for ref in nair_refs)      # payments channel
    assert all("C200" in ref for ref in shah_refs)       # legal-ops channel


def test_deterministic_rerun_produces_identical_mapping(sample_hr, sample_slack, sample_jira, sample_github):
    run_a = re_mod.run(sample_hr, sample_slack, sample_jira, sample_github, output_dir=None)
    run_b = re_mod.run(sample_hr, sample_slack, sample_jira, sample_github, output_dir=None)
    assert run_a == run_b


# --------------------------------------------------------------------------------------
# Unit tests for the resemblance / candidate-link heuristics in isolation
# --------------------------------------------------------------------------------------

def test_token_resembles_exact_and_initial():
    assert token_resembles("kumar", "kumar")
    assert token_resembles("k", "kumar")       # initial vs. full surname
    assert token_resembles("kumar", "k")       # symmetric
    assert not token_resembles("kumar", "chen")


def _hr_node(surrogate_id, display_name, team, **fields):
    """Build a PersonNode the way build_nodes() would, from a partial HR-row-like dict."""
    node = PersonNode(surrogate_id=surrogate_id, display_name=display_name, team=team)
    for ftype in ("emp_id", "work_email", "slack_handle", "github_login"):
        if ftype in fields:
            node.add(Identifier("hr", ftype, fields[ftype], 1, "authoritative", 1.0, "test", "hr.csv"))
    return node


def test_candidate_link_requires_surname_or_login_anchor_not_bare_first_initial():
    """Regression test: an earlier version flagged 'Alice Chen' against 'A. Kumar' purely because
    both first names reduce to the letter 'a'. A bare first-initial match must never be the sole
    trigger for a candidate link."""
    alice = _hr_node("PERSON_0001", "Alice Chen", "payments", emp_id="1", work_email="alice@acme.com",
                      slack_handle="U1", github_login="achen")
    a_kumar = _hr_node("PERSON_0002", "A. Kumar", "contractors", work_email="akumar@gmail.com",
                        github_login="akumar-ext")
    links = candidate_links({"PERSON_0001": alice, "PERSON_0002": a_kumar})
    assert links == []


def test_candidate_link_fires_on_surname_resemblance_between_weak_nodes():
    alex_kumar = _hr_node("PERSON_0001", "Alex Kumar", "payments", emp_id="1", work_email="alex.kumar@acme.com",
                           slack_handle="U1", github_login="akumar-acme")
    a_kumar = _hr_node("PERSON_0002", "A. Kumar", "contractors", work_email="akumar@gmail.com",
                        github_login="akumar-ext")
    links = candidate_links({"PERSON_0001": alex_kumar, "PERSON_0002": a_kumar})
    assert len(links) == 1
    assert links[0]["status"] == "open"


def test_candidate_link_skipped_when_both_nodes_fully_attested():
    """Two complete, independently-verified HR rows sharing a surname-ish resemblance should still
    not be flagged -- is_weakly_attested() gates the whole check to avoid noise on fully-attested
    people."""
    full_a = _hr_node("PERSON_0001", "Priya Nair", "payments", emp_id="1", work_email="priya.nair@acme.com",
                       slack_handle="U1", github_login="pnair")
    full_b = _hr_node("PERSON_0002", "Priya Shah", "legal", emp_id="2", work_email="priya.shah@acme.com",
                       slack_handle="U2", github_login="pshah")
    assert not is_weakly_attested(full_a)
    assert not is_weakly_attested(full_b)
    assert candidate_links({"PERSON_0001": full_a, "PERSON_0002": full_b}) == []


def test_ambiguous_bare_name_with_no_channel_context_stays_unresolved():
    """Two candidates share a first name; the message's channel has no recorded team votes at all
    -> must land in unresolved_mentions, never guessed."""
    nodes = {
        "PERSON_0001": _hr_node("PERSON_0001", "Sam Diaz", "sales"),
        "PERSON_0002": _hr_node("PERSON_0002", "Sam Lee", "support"),
    }
    idx = {"by_email": {}, "by_full_name": {}, "full_name_patterns": [],
           "by_first_name": {"sam": ["PERSON_0001", "PERSON_0002"]}}
    unresolved = []
    scan_free_text(nodes, idx, "slack", "slack:C999:t1", "cc Sam on this one", "unmapped-channel",
                    channel_team_votes={}, unresolved=unresolved)
    assert len(unresolved) == 1
    assert isinstance(unresolved[0], UnresolvedMention)
    assert unresolved[0].reason_code == "ambiguous_multi_candidate"


def test_unambiguous_bare_name_resolves_without_context():
    """A first name that's unique across the whole org resolves even without channel corroboration."""
    nodes = {"PERSON_0001": _hr_node("PERSON_0001", "Jordan Lee", "legal")}
    idx = {"by_email": {}, "by_full_name": {}, "full_name_patterns": [],
           "by_first_name": {"jordan": ["PERSON_0001"]}}
    unresolved = []
    scan_free_text(nodes, idx, "slack", "slack:C1:t1", "Jordan will follow up", None,
                    channel_team_votes={}, unresolved=unresolved)
    assert unresolved == []
    assert any(i.type == "free_text_name_unique" for i in nodes["PERSON_0001"].identifiers)


# --------------------------------------------------------------------------------------
# Regression test for the compile-hoist performance fix: full-name regexes are precomputed
# once by build_nodes(), not recompiled per message inside scan_free_text().
# --------------------------------------------------------------------------------------

def test_full_name_patterns_precomputed_once_and_match_correctly(sample_hr):
    _nodes, idx, _registry = re_mod.build_nodes(sample_hr, registry=None)
    assert "full_name_patterns" in idx
    # 7 people, all with distinct full display names -> all 7 should have a precompiled pattern.
    assert len(idx["full_name_patterns"]) == 7
    assert all(hasattr(pattern, "search") for pattern, _sid in idx["full_name_patterns"])

    # And they still actually match, exercised the same way scan_free_text uses them.
    matches = [sid for pattern, sid in idx["full_name_patterns"] if pattern.search("Alice Chen said hi")]
    assert len(matches) == 1


def test_duplicate_full_names_excluded_from_precomputed_patterns():
    """Two different people sharing the exact same full display name must not get a precompiled
    (necessarily ambiguous) pattern -- same exclusion the old per-message loop applied via
    `len(sids) != 1`, just computed once instead of on every call."""
    rows = [
        {"emp_id": "1", "display_name": "Sam Lee", "work_email": "sam1@acme.com",
         "slack_handle": "", "github_login": "", "team": "eng"},
        {"emp_id": "2", "display_name": "Sam Lee", "work_email": "sam2@acme.com",
         "slack_handle": "", "github_login": "", "team": "eng"},
    ]
    _nodes, idx, _registry = re_mod.build_nodes(rows, registry=None)
    assert idx["full_name_patterns"] == []
