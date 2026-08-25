#!/usr/bin/env python3
"""
Consistent de-identification.

Reuses the *exact* matches recorded by resolve_entities.py (output/er_mapping.json) rather than
re-deriving them — this is what guarantees "same person, same surrogate, everywhere" (FR-8): there
is exactly one place that decides who a mention is, and this script only ever applies that decision.

Two kinds of substitution:
  - structured fields (Slack `user`, Jira `assignee_email`/comment `author`, GitHub PR `author` /
    commit `author`) -> replaced wholesale with the surrogate id.
  - free text (message/comment bodies, PR titles, commit messages) -> only the exact literal spans
    resolve_entities.py already matched and attributed are replaced, longest-match-first so e.g.
    "Alice Chen" doesn't get partially clobbered by a shorter overlapping pattern. This is
    context-safe: a bare "Priya" in one message and a bare "Priya" in another can (and do, in this
    sample) resolve to two different surrogates, because substitution is keyed per source record,
    not by a single global string->surrogate table.

Any mention resolve_entities.py logged as unresolved is replaced with the literal placeholder
[UNRESOLVED_PERSON] wherever its raw text appears in that record — never a guessed surrogate (FR-8).

`run()` does the work and returns {"slack": [...], "jira": [...], "github": [...]}; `main()` is the
thin CLI entrypoint (`poetry run deidentify`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths

STRUCTURED_TYPES = {
    "user_id", "assignee_email", "comment_author_email", "comment_author_name",
    "pr_author", "commit_author_login", "commit_author_email",
}
PLACEHOLDER = "[UNRESOLVED_PERSON]"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def index_er_mapping(mapping):
    """record_ref -> {"structured": surrogate_id or None,
                       "free_text": [(literal_value, surrogate_id), ...] sorted longest-first}"""
    by_ref: dict[str, dict] = {}
    for person in mapping["people"]:
        sid = person["surrogate_id"]
        for ident in person["identifiers"]:
            ref = ident["record_ref"]
            if not ref or ident["source"] == "hr":
                continue
            entry = by_ref.setdefault(ref, {"structured": None, "free_text": []})
            if ident["type"] in STRUCTURED_TYPES:
                entry["structured"] = sid
            elif ident["type"].startswith("free_text_") or ident["type"] == "bare_first_name_contextual":
                entry["free_text"].append((ident["value"], sid))

    for entry in by_ref.values():
        entry["free_text"].sort(key=lambda t: len(t[0]), reverse=True)

    unresolved_by_ref: dict[str, list[str]] = {}
    for u in mapping["unresolved_mentions"]:
        unresolved_by_ref.setdefault(u["record_ref"], []).append(u["raw_text"])

    return by_ref, unresolved_by_ref


def scrub_text(text: str, ref: str, by_ref: dict, unresolved_by_ref: dict) -> str:
    if text is None:
        return text
    for raw in unresolved_by_ref.get(ref, []):
        text = re.sub(re.escape(raw), PLACEHOLDER, text)
    for literal, sid in by_ref.get(ref, {}).get("free_text", []):
        # Leading \b only: `literal` is the exact span resolve_entities.py already matched, which
        # can legitimately end in a non-word character (e.g. the "." in the abbreviation "Alex K."),
        # and a trailing \b would fail to match right after such a character.
        text = re.sub(r"\b" + re.escape(literal), sid, text, flags=re.IGNORECASE)
    return text


def structured(ref: str, by_ref: dict, fallback: str) -> str:
    entry = by_ref.get(ref)
    if entry and entry["structured"]:
        return entry["structured"]
    return fallback  # no deterministic match recorded for this exact record_ref/type — leave a
                      # visible marker rather than the raw identifier, since a raw fallback would
                      # silently leak PII that resolve_entities.py failed to attribute.


def deidentify_slack(msgs, by_ref, unresolved_by_ref):
    out = []
    for m in msgs:
        ref = f"slack:{m['channel']}:{m['ts']}"
        out.append({
            **{k: v for k, v in m.items() if k not in ("user", "text")},
            "user": structured(ref, by_ref, PLACEHOLDER),
            "text": scrub_text(m["text"], ref, by_ref, unresolved_by_ref),
        })
    return out


def deidentify_jira(issues, by_ref, unresolved_by_ref):
    out = []
    for issue in issues:
        assignee_ref = f"jira:{issue['key']}:assignee"
        comments = []
        for c_i, comment in enumerate(issue.get("comments", [])):
            c_ref = f"jira:{issue['key']}:comment[{c_i}]"
            comments.append({
                "author": structured(c_ref, by_ref, PLACEHOLDER),
                "body": scrub_text(comment["body"], c_ref, by_ref, unresolved_by_ref),
            })
        out.append({
            "key": issue["key"],
            "status": issue["status"],
            "assignee": structured(assignee_ref, by_ref, PLACEHOLDER),
            "export_eligible": issue.get("export_eligible", False),
            "comments": comments,
        })
    return out


def deidentify_github(prs, by_ref, unresolved_by_ref):
    out = []
    for pr in prs:
        author_ref = f"github:pr#{pr['pr']}:author"
        title_ref = f"github:pr#{pr['pr']}:title"
        commits = []
        for commit in pr.get("commits", []):
            c_ref = f"github:pr#{pr['pr']}:commit:{commit['sha']}"
            commits.append({
                "sha": commit["sha"],
                "author": structured(c_ref, by_ref, PLACEHOLDER),
                "message": scrub_text(commit["message"], c_ref, by_ref, unresolved_by_ref),
            })
        out.append({
            "pr": pr["pr"],
            "status": pr["status"],
            "title": scrub_text(pr["title"], title_ref, by_ref, unresolved_by_ref),
            "author": structured(author_ref, by_ref, PLACEHOLDER),
            "commits": commits,
        })
    return out


def run(mapping=None, slack_msgs=None, jira_issues=None, github_prs=None, output_dir=None) -> dict:
    """Run de-identification end to end and return {"slack": [...], "jira": [...], "github": [...]}.

    `mapping` defaults to loading output/er_mapping.json; the three source lists default to loading
    sample_data/. If `output_dir` is given, the three de-identified files are written there.
    """
    mapping = load_json(paths.ER_MAPPING_JSON) if mapping is None else mapping
    slack_msgs = load_json(paths.SLACK_JSON) if slack_msgs is None else slack_msgs
    jira_issues = load_json(paths.JIRA_JSON) if jira_issues is None else jira_issues
    github_prs = load_json(paths.GITHUB_JSON) if github_prs is None else github_prs

    by_ref, unresolved_by_ref = index_er_mapping(mapping)
    result = {
        "slack": deidentify_slack(slack_msgs, by_ref, unresolved_by_ref),
        "jira": deidentify_jira(jira_issues, by_ref, unresolved_by_ref),
        "github": deidentify_github(github_prs, by_ref, unresolved_by_ref),
    }

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("slack", "jira", "github"):
            with open(output_dir / f"{name}.json", "w", encoding="utf-8") as f:
                json.dump(result[name], f, indent=2)

    return result


def main():
    run(output_dir=paths.DEIDENT_DIR)
    print(f"Wrote de-identified output to {paths.DEIDENT_DIR}")


if __name__ == "__main__":
    main()
