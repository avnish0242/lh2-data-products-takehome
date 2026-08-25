"""Decision-unit assembly (FR-9/FR-10): sell-safety policy filtering and the sellable/audit split."""
import json

from lh2_pipeline.assemble_decision_unit import policy_check

from conftest import surrogate_for


# --------------------------------------------------------------------------------------
# policy_check() in isolation
# --------------------------------------------------------------------------------------

def test_private_channel_excluded():
    allowed, reason = policy_check(channel_visibility="private", text="anything")
    assert not allowed and "private_channel" in reason


def test_public_channel_allowed():
    allowed, reason = policy_check(channel_visibility="public", text="ordinary message")
    assert allowed and reason is None


def test_export_ineligible_excluded():
    allowed, reason = policy_check(export_eligible=False)
    assert not allowed and reason == "export_ineligible"


def test_confidential_language_excluded_even_without_visibility_flag():
    """Content-level detection is a second, independent net -- it must fire even when no
    channel_visibility is given at all (e.g. a Jira comment)."""
    allowed, reason = policy_check(text="This is confidential, keep it quiet please")
    assert not allowed and reason == "customer_confidential_content"


def test_clean_public_content_allowed():
    allowed, reason = policy_check(channel_visibility="public", export_eligible=True,
                                    text="fixed the null pointer bug")
    assert allowed


# --------------------------------------------------------------------------------------
# Whole decision unit, against the real sample
# --------------------------------------------------------------------------------------

def test_private_globex_message_excluded_from_sellable_unit(decision_unit_and_audit):
    decision_unit, audit = decision_unit_and_audit
    sellable_text = json.dumps(decision_unit)
    assert "globex" not in sellable_text.lower()
    assert "dispute" not in sellable_text.lower()


def test_excluded_content_only_lives_in_the_audit_object(decision_unit_and_audit):
    decision_unit, audit = decision_unit_and_audit
    assert "excluded_events" not in decision_unit
    assert len(audit["excluded_events"]) == 1
    assert "private_channel" in audit["excluded_events"][0]["reason"]
    assert audit["classification"].startswith("INTERNAL ONLY")


def test_next_day_offtopic_message_not_pulled_into_the_thread(er_mapping, decision_unit_and_audit):
    """The contractor's next-day 'happy to help' note is same-channel but a different day than the
    incident -- it shouldn't be treated as part of this decision unit at all (not timeline, not
    excluded_events, since it was never policy-rejected -- it's just out of scope)."""
    decision_unit, audit = decision_unit_and_audit
    alex_k = surrogate_for(er_mapping, type_="slack_handle", value="UNK_ALEXK")
    all_actors = {e["actor"] for e in decision_unit["timeline"]}
    assert alex_k not in all_actors


def test_participants_include_expected_roles(er_mapping, decision_unit_and_audit):
    decision_unit, _audit = decision_unit_and_audit
    alice = surrogate_for(er_mapping, type_="work_email", value="alice.chen@acme.com")
    contractor = surrogate_for(er_mapping, type_="work_email", value="akumar@gmail.com")

    roles_by_person = {p["surrogate_id"]: set(p["roles"]) for p in decision_unit["participants"]}
    assert "reporter" in roles_by_person[alice]
    assert "author" in roles_by_person[alice]        # PR author + commit author
    assert "contributor" in roles_by_person[contractor]  # the akumar@gmail.com regression-test commit


def test_every_timeline_event_has_a_citation(decision_unit_and_audit):
    decision_unit, _audit = decision_unit_and_audit
    for event in decision_unit["timeline"]:
        assert event["citation"]["source_system"] in {"slack", "jira", "github"}
        assert event["citation"]["record_id"]
