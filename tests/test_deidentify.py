"""Consistent de-identification (FR-8): same person -> same surrogate everywhere, unresolved
mentions -> placeholder, never a guess."""
import json
import re

from lh2_pipeline import deidentify

from conftest import surrogate_for


# --------------------------------------------------------------------------------------
# Whole-sample behavior
# --------------------------------------------------------------------------------------

def test_same_person_same_surrogate_across_all_three_sources(er_mapping, deid_result):
    alice = surrogate_for(er_mapping, type_="work_email", value="alice.chen@acme.com")

    # Alice's own Slack message: structured `user` field.
    her_message = next(m for m in deid_result["slack"] if "500 on empty cart" in m["text"])
    assert her_message["user"] == alice
    # A colleague's reply mentions her by email in free text -> same surrogate.
    reply = next(m for m in deid_result["slack"] if "pair with" in m["text"])
    assert alice in reply["text"]

    jira_issue = deid_result["jira"][0]
    assert jira_issue["assignee"] == alice  # structured assignee_email field

    github_pr = deid_result["github"][0]
    assert github_pr["author"] == alice                  # "Alice Chen <alice.chen@acme.com>"
    assert github_pr["commits"][0]["author"] == alice     # commit a1b2, author "achen"


def test_no_raw_hr_identifiers_leak_into_deidentified_output(sample_hr, deid_result):
    """No display name, email, slack handle, or github login from the roster should survive,
    anywhere, structured or free text."""
    haystack = json.dumps(deid_result)
    for row in sample_hr:
        for field in ("display_name", "work_email", "slack_handle", "github_login"):
            if row[field]:
                assert not re.search(r"\b" + re.escape(row[field]) + r"\b", haystack, re.IGNORECASE), \
                    f"raw {field}={row[field]!r} leaked into de-identified output"


def test_bare_name_substitution_is_per_message_not_global(er_mapping, deid_result):
    """The two bare 'Priya' mentions must become two *different* surrogates in their respective
    messages -- proof that substitution isn't a single global string->id table."""
    priya_nair = surrogate_for(er_mapping, type_="work_email", value="priya.nair@acme.com")
    priya_shah = surrogate_for(er_mapping, type_="work_email", value="priya.shah@acme.com")

    payments_msg = next(m for m in deid_result["slack"] if m["channel"] == "C100" and "review" in m["text"] and m["user"] != priya_nair)
    legal_msg = next(m for m in deid_result["slack"] if m["channel"] == "C200" and "Globex" in m["text"])

    assert priya_nair in payments_msg["text"]
    assert priya_shah in legal_msg["text"]
    assert priya_shah not in payments_msg["text"]
    assert priya_nair not in legal_msg["text"]


def test_abbreviation_period_not_left_stranded(deid_result):
    """Regression test for the 'PERSON_0006. here...' cosmetic bug: redacting an abbreviated name
    like 'Alex K.' must consume the trailing period, not leave it dangling after the surrogate."""
    msg = next(m for m in deid_result["slack"] if "contractor side" in m["text"])
    assert ". here" not in msg["text"]
    assert re.match(r"^PERSON_\d{4} here", msg["text"])


# --------------------------------------------------------------------------------------
# Unit tests for the substitution primitives in isolation
# --------------------------------------------------------------------------------------

def test_unresolved_mention_becomes_placeholder_not_a_guessed_surrogate():
    mapping = {
        "people": [{
            "surrogate_id": "PERSON_0001",
            "identifiers": [{
                "source": "slack", "type": "user_id", "value": "U1", "record_ref": "slack:C1:t1",
            }],
        }],
        "unresolved_mentions": [{
            "record_ref": "slack:C1:t1", "raw_text": "Sam", "reason_code": "ambiguous_multi_candidate",
        }],
    }
    by_ref, unresolved_by_ref = deidentify.index_er_mapping(mapping)
    scrubbed = deidentify.scrub_text("cc Sam on this", "slack:C1:t1", by_ref, unresolved_by_ref)
    assert scrubbed == "cc [UNRESOLVED_PERSON] on this"
    assert "PERSON_0001" not in scrubbed  # never silently mapped to the only known person in the record


def test_structured_falls_back_to_placeholder_when_no_deterministic_match():
    assert deidentify.structured("slack:unknown:t9", {}, deidentify.PLACEHOLDER) == deidentify.PLACEHOLDER


def test_free_text_substitution_is_longest_match_first():
    """'Alice Chen' (a longer, more specific match) must win over a shorter overlapping pattern
    rather than leaving a fragment of the name behind."""
    by_ref = {"ref1": {"structured": None, "free_text": [("Alice Chen", "PERSON_0001"), ("Alice", "PERSON_0001")]}}
    result = deidentify.scrub_text("Alice Chen took the ticket", "ref1", by_ref, {})
    assert result == "PERSON_0001 took the ticket"
