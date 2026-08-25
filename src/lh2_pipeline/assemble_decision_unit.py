#!/usr/bin/env python3
"""
Assemble one decision unit: PAY-123, "bug reported in Slack -> tracked in Jira -> fixed in a
merged PR" (the assignment's own example, and the only complete cross-system thread in the sample).

Reads ONLY the already de-identified sources (output/deidentified/*.json) plus their retained
non-PII policy fields (channel_visibility, export_eligible) -- the assembler never touches raw PII,
which is a deliberate ordering choice: de-identify first, assemble second, so a bug in assembly
logic can leak at most a surrogate id, never a name/email/handle.

Correlation key: PAY-123 mentions plus same-day payments-channel (C100) messages (thread context).
This is a hand-picked, explicit correlation for this sample -- see architecture.md Sec 4.1 for how
this generalizes to an issue-key/thread-linkage correlation engine at scale.

Sell-safety policy (independent of de-identification -- FR-10):
  - channel_visibility != "public"        -> excluded
  - export_eligible == False (if present) -> excluded
  - customer-confidentiality language      -> excluded (coarse keyword net; see requirements_analysis.md
                                               Sec 5 for why this needs a real classifier at scale)
Excluded events are written to a SEPARATE internal-audit object, never into the sellable decision
unit itself -- a prior draft of this script kept them in an `excluded_events` field on the same
object, which defeated the purpose (the excluded text was still sitting in the "sellable" file).
Splitting the output is what actually enforces "include only what is safe to sell."

`run()` does the work and returns (decision_unit, audit); `main()` is the thin CLI entrypoint
(`poetry run assemble-decision-unit`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths

CONFIDENTIALITY_PATTERN = re.compile(
    r"\b(dispute|confidential|keep.*quiet|do not disclose|off the record|nda)\b", re.IGNORECASE
)
PAY_123 = "PAY-123"
PAYMENTS_CHANNEL = "C100"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def policy_check(*, channel_visibility=None, export_eligible=None, text=""):
    """Returns (allowed: bool, reason: str|None)."""
    if channel_visibility is not None and channel_visibility != "public":
        return False, f"private_channel (channel_visibility={channel_visibility!r})"
    if export_eligible is False:
        return False, "export_ineligible"
    if CONFIDENTIALITY_PATTERN.search(text or ""):
        return False, "customer_confidential_content"
    return True, None


def build_decision_unit(slack_msgs, jira_issues, github_prs):
    """Pure function: de-identified sources in, (decision_unit, audit) out. No file I/O."""
    timeline = []
    excluded = []

    # --- Slack: PAY-123 mentions, plus same-day payments-channel messages (thread context) ---
    # A same-channel message from a *different day* (the contractor's next-day "happy to help"
    # note) is judged too temporally distant to be part of this specific incident thread, even
    # though it's in the same channel -- this is exactly the kind of contextual/temporal call
    # flagged as an assumption in requirements_analysis.md.
    pay123_dates = {m["ts"][:10] for m in slack_msgs if PAY_123 in m["text"]}
    for m in slack_msgs:
        relevant = PAY_123 in m["text"] or (m["channel"] == PAYMENTS_CHANNEL and m["ts"][:10] in pay123_dates)
        if not relevant:
            continue
        allowed, reason = policy_check(channel_visibility=m["channel_visibility"], text=m["text"])
        citation = {"source_system": "slack", "record_id": f"{m['channel']}@{m['ts']}"}
        if not allowed:
            excluded.append({"citation": citation, "reason": reason, "note": m["text"]})
            continue
        timeline.append({
            "ts": m["ts"],
            "event_type": "slack_message",
            "actor": m["user"],
            "role": "reporter" if m is slack_msgs[0] else "participant",
            "detail": m["text"],
            "citation": citation,
        })

    # --- Jira: the issue itself + its comments ---
    for issue in jira_issues:
        if issue["key"] != PAY_123:
            continue
        allowed, reason = policy_check(export_eligible=issue.get("export_eligible"))
        citation = {"source_system": "jira", "record_id": f"{issue['key']}"}
        if not allowed:
            excluded.append({"citation": citation, "reason": reason, "note": "issue-level"})
        else:
            timeline.append({
                "ts": None,  # not present in source data -- ordered by pipeline stage, not clock
                            # time; see module docstring / requirements_analysis.md open questions.
                "event_type": "jira_assignment",
                "actor": issue["assignee"],
                "role": "assignee",
                "detail": f"{issue['key']} status={issue['status']}",
                "citation": citation,
            })
        for c_i, comment in enumerate(issue.get("comments", [])):
            c_citation = {"source_system": "jira", "record_id": f"{issue['key']}#comment[{c_i}]"}
            allowed, reason = policy_check(text=comment["body"])
            if not allowed:
                excluded.append({"citation": c_citation, "reason": reason, "note": comment["body"]})
                continue
            timeline.append({
                "ts": None,
                "event_type": "jira_comment",
                "actor": comment["author"],
                "role": "reviewer" if "review" in comment["body"].lower() else "participant",
                "detail": comment["body"],
                "citation": c_citation,
            })

    # --- GitHub: the PR + its commits ---
    for pr in github_prs:
        if PAY_123 not in pr["title"]:
            continue
        citation = {"source_system": "github", "record_id": f"pr#{pr['pr']}"}
        timeline.append({
            "ts": None,
            "event_type": "pr_merged" if pr["status"] == "merged" else "pr_opened",
            "actor": pr["author"],
            "role": "author",
            "detail": pr["title"],
            "citation": citation,
        })
        for commit in pr.get("commits", []):
            c_citation = {"source_system": "github", "record_id": f"pr#{pr['pr']}:{commit['sha']}"}
            timeline.append({
                "ts": None,
                "event_type": "commit",
                "actor": commit["author"],
                "role": "author" if commit["author"] == pr["author"] else "contributor",
                "detail": commit["message"],
                "citation": c_citation,
            })

    participants = {}
    for event in timeline:
        if event["actor"] and event["actor"] != "[UNRESOLVED_PERSON]":
            participants.setdefault(event["actor"], set()).add(event["role"])

    # --- The sellable object: no excluded content, no raw text from anything policy rejected. ---
    decision_unit = {
        "decision_unit_id": "DU_PAY-123",
        "summary": "Null-cart 500 error on the payments page, reported in Slack, tracked as "
                    "Jira PAY-123, reviewed, and fixed in merged GitHub PR #88.",
        "correlation_method": "explicit: Slack channel C100 thread (same-day) + any record naming "
                               "'PAY-123'",
        "timeline": timeline,
        "participants": [
            {"surrogate_id": sid, "roles": sorted(roles)} for sid, roles in participants.items()
        ],
    }

    # --- Internal-only audit trail: WHY things were excluded, kept separately from the product. ---
    # Even here the raw excluded text is retained deliberately (audit needs to be able to answer
    # "what exactly got dropped and why" -- see NFR-3) but this object must never ship to a customer
    # and should live under the same access control as the identity crosswalk (NFR-5).
    audit = {
        "decision_unit_id": "DU_PAY-123",
        "classification": "INTERNAL ONLY -- do not include in any sellable export",
        "excluded_events": excluded,
    }

    return decision_unit, audit


def run(slack_msgs=None, jira_issues=None, github_prs=None, output_dir=None):
    """Run decision-unit assembly end to end and return (decision_unit, audit).

    Inputs default to loading the already de-identified files under output/deidentified/. If
    `output_dir` is given, both objects are written there as separate files.
    """
    slack_msgs = load_json(paths.DEIDENT_SLACK_JSON) if slack_msgs is None else slack_msgs
    jira_issues = load_json(paths.DEIDENT_JIRA_JSON) if jira_issues is None else jira_issues
    github_prs = load_json(paths.DEIDENT_GITHUB_JSON) if github_prs is None else github_prs

    decision_unit, audit = build_decision_unit(slack_msgs, jira_issues, github_prs)

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "decision_unit_pay123.json", "w", encoding="utf-8") as f:
            json.dump(decision_unit, f, indent=2)
        with open(output_dir / "decision_unit_pay123.internal_audit.json", "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)

    return decision_unit, audit


def main():
    decision_unit, audit = run(output_dir=paths.OUTPUT)
    print(f"Wrote {paths.DECISION_UNIT_JSON} -- {len(decision_unit['timeline'])} events, "
          f"{len(decision_unit['participants'])} participants")
    print(f"Wrote {paths.DECISION_UNIT_AUDIT_JSON} (INTERNAL ONLY) -- "
          f"{len(audit['excluded_events'])} excluded events")


if __name__ == "__main__":
    main()
