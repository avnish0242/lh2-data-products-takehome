#!/usr/bin/env python3
"""
Entity resolution — the core deliverable.

Builds an identity graph rooted in hr_directory.csv, then matches every mention in
slack.json / jira.json / github.json against it in strict, descending trust order:

    tier 1  emp_id                  (not referenced outside HR in this sample, but the anchor
                                      identifier for future sources)
    tier 2  email                   (work email or any raw email string, exact + case-insensitive)
    tier 3  platform user id        (Slack `user` field <-> HR `slack_handle`)
    tier 4  github_login            (commit/PR author handle <-> HR `github_login`)
    tier 5  name                    (full-name exact match is unambiguous-but-weak; a bare first
                                      name is only resolved when a same-channel team-affinity signal
                                      disambiguates it — see resolve_bare_first_names())

Two HR rows are NEVER auto-merged on name/handle resemblance alone (see requirements_analysis.md
FR-4). Where resemblance exists without a shared strong identifier, a CandidateLink is recorded,
flagged `open`, and never applied. See candidate_links().

`run()` does the actual work and returns the ER mapping dict (optionally writing it to disk);
`main()` is the thin CLI entrypoint (`poetry run resolve-entities`). The split exists so tests can
call `run()` against small in-memory fixtures without touching the filesystem.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from itertools import combinations
from pathlib import Path

from . import paths

TIER_EMP_ID, TIER_EMAIL, TIER_SLACK_ID, TIER_GITHUB_LOGIN, TIER_NAME = 1, 2, 3, 4, 5

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PR_AUTHOR_RE = re.compile(r"^(?P<name>[^<]+?)\s*<(?P<email>[^>]+)>\s*$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")


# --------------------------------------------------------------------------------------
# Identity graph model
# --------------------------------------------------------------------------------------

@dataclass
class Identifier:
    source: str            # "hr" | "slack" | "jira" | "github"
    type: str               # e.g. "emp_id", "work_email", "slack_user_id", "github_login",
                             # "free_text_email", "free_text_full_name", "bare_first_name_contextual"
    value: str
    tier: int
    method: str              # "authoritative" | "exact" | "contextual"
    confidence: float
    evidence: str
    record_ref: str = ""     # pointer back to the source record this edge came from


@dataclass
class PersonNode:
    surrogate_id: str
    display_name: str
    team: str
    identifiers: list = field(default_factory=list)

    def add(self, ident: Identifier) -> None:
        # de-dupe identical (type, value, record_ref) edges
        key = (ident.type, ident.value, ident.record_ref)
        if any((i.type, i.value, i.record_ref) == key for i in self.identifiers):
            return
        self.identifiers.append(ident)


@dataclass
class UnresolvedMention:
    source: str
    record_ref: str
    raw_text: str
    reason_code: str
    note: str = ""


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------

def load_hr(path=None):
    with open(path or paths.HR_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def norm_email(s: str) -> str:
    return s.strip().lower()


def norm_name(s: str) -> str:
    return re.sub(r"[.\s]+", " ", s.strip().lower()).strip()


# --------------------------------------------------------------------------------------
# Identity registry — stable surrogate-ID assignment across delta/incremental runs
# --------------------------------------------------------------------------------------
#
# See docs/architecture.md Sec 7.1. Surrogate IDs must NOT be derived from HR row position: a new
# hire inserted mid-file, or a re-exported roster in a different row order, would otherwise
# reshuffle every ID after the change point. Instead, each row is looked up against a persisted
# registry using the same tiered-trust order already used for mention matching
# (emp_id > work_email > slack_handle > github_login), falling back to a (display_name, team) key
# -- explicitly the weakest, least trustworthy tier -- only when a row has no stronger identifier
# at all. A row that matches nothing in the registry mints a new, sequentially-numbered surrogate,
# which is appended, never inserted or reassigned.

RegistryIndices = tuple  # (by_emp_id, by_email, by_slack_id, by_github_login, by_name_team)


def empty_registry() -> dict:
    return {"next_seq": 1, "people": []}


def load_registry(path) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_registry()
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry: dict, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)


def registry_indices(registry: dict) -> RegistryIndices:
    by_emp_id, by_email, by_slack_id, by_github_login, by_name_team = {}, {}, {}, {}, {}
    for person in registry["people"]:
        sid = person["surrogate_id"]
        if person.get("emp_id"):
            by_emp_id[person["emp_id"]] = sid
        if person.get("work_email"):
            by_email[norm_email(person["work_email"])] = sid
        if person.get("slack_handle"):
            by_slack_id[person["slack_handle"]] = sid
        if person.get("github_login"):
            by_github_login[person["github_login"]] = sid
        if person.get("display_name") and person.get("team"):
            by_name_team[(norm_name(person["display_name"]), person["team"])] = sid
    return by_emp_id, by_email, by_slack_id, by_github_login, by_name_team


def assign_surrogate_id(row: dict, registry: dict, indices: RegistryIndices) -> tuple[str, bool, str]:
    """Return (surrogate_id, is_new_person, match_tier) for this HR row against the registry.

    The (name, team) fallback tier is a best-effort continuity heuristic for rows with no durable
    identifier at all -- it cannot actually distinguish "the same sparse person reappearing" from
    "a different person who happens to share a name and team". That's a named limitation (see
    docs/architecture.md Sec 7.1), not a solved problem: there is no way to guarantee identity
    continuity for a person with zero durable identifiers across separate ingestion runs.
    """
    by_emp_id, by_email, by_slack_id, by_github_login, by_name_team = indices

    sid, tier = None, None
    if row["emp_id"] and row["emp_id"] in by_emp_id:
        sid, tier = by_emp_id[row["emp_id"]], "emp_id"
    elif row["work_email"] and norm_email(row["work_email"]) in by_email:
        sid, tier = by_email[norm_email(row["work_email"])], "work_email"
    elif row["slack_handle"] and row["slack_handle"] in by_slack_id:
        sid, tier = by_slack_id[row["slack_handle"]], "slack_handle"
    elif row["github_login"] and row["github_login"] in by_github_login:
        sid, tier = by_github_login[row["github_login"]], "github_login"
    else:
        name_team_key = ((norm_name(row["display_name"]), row["team"])
                          if row["display_name"] and row["team"] else None)
        if name_team_key and name_team_key in by_name_team:
            sid, tier = by_name_team[name_team_key], "name_team (weak fallback)"

    is_new = sid is None
    if is_new:
        sid = f"PERSON_{registry['next_seq']:04d}"
        registry["next_seq"] += 1
        entry = {"surrogate_id": sid, "emp_id": None, "work_email": None, "slack_handle": None,
                  "github_login": None, "display_name": None, "team": None}
        registry["people"].append(entry)
        tier = "new"
    else:
        entry = next(p for p in registry["people"] if p["surrogate_id"] == sid)

    # Enrich: fill in any field this row newly carries that the registry didn't have before (e.g.
    # a contractor row that later gains a work_email), and keep the lookup indices current so
    # later rows in the same batch -- and later runs -- can match on whichever identifier the row
    # happens to carry. Never overwrite an already-recorded value; only fill gaps.
    for field, idx_map, norm in (("emp_id", by_emp_id, lambda v: v),
                                  ("work_email", by_email, norm_email),
                                  ("slack_handle", by_slack_id, lambda v: v),
                                  ("github_login", by_github_login, lambda v: v)):
        if row[field]:
            entry[field] = entry[field] or row[field]
            idx_map[norm(row[field])] = sid
    if row["display_name"] and row["team"]:
        entry["display_name"] = entry["display_name"] or row["display_name"]
        entry["team"] = entry["team"] or row["team"]
        by_name_team[(norm_name(row["display_name"]), row["team"])] = sid

    return sid, is_new, tier


# --------------------------------------------------------------------------------------
# Build identity graph from HR (the authoritative root — FR-2)
# --------------------------------------------------------------------------------------

def build_nodes(hr_rows, registry: dict | None = None):
    """Build the in-memory identity graph for this run.

    Surrogate IDs come from `registry` (see assign_surrogate_id() above) rather than row position,
    so re-ordering hr_directory.csv or inserting a new hire mid-file does not reshuffle existing
    people's surrogate ids. `registry=None` (the default) means a one-shot, non-persistent run:
    every row is "new" and gets PERSON_0001, PERSON_0002, ... in file order -- exactly today's
    behavior when there's no prior registry to consult, e.g. every existing test that doesn't care
    about delta ingestion.

    Returns (nodes_by_surrogate_id, mention_matching_indices, registry).
    """
    if registry is None:
        registry = empty_registry()
    reg_idx = registry_indices(registry)

    # dict, not list: if two rows in this batch resolve to the same registry surrogate (duplicate
    # rows, or a data-quality collision), their identifier edges get merged onto one node instead
    # of the second row's PersonNode silently clobbering the first's in the final lookup dict.
    nodes: dict[str, PersonNode] = {}
    by_emp_id, by_email, by_slack_id, by_github_login = {}, {}, {}, {}
    by_full_name, by_first_name = {}, {}

    for row in hr_rows:
        surrogate_id, _is_new, _tier = assign_surrogate_id(row, registry, reg_idx)
        node = nodes.get(surrogate_id)
        if node is None:
            node = PersonNode(surrogate_id=surrogate_id, display_name=row["display_name"], team=row["team"])
            nodes[surrogate_id] = node

        if row["emp_id"]:
            node.add(Identifier("hr", "emp_id", row["emp_id"], TIER_EMP_ID, "authoritative", 1.0,
                                 "HR roster row (authoritative root)", "hr_directory.csv"))
            by_emp_id[row["emp_id"]] = node.surrogate_id
        if row["work_email"]:
            e = norm_email(row["work_email"])
            node.add(Identifier("hr", "work_email", row["work_email"], TIER_EMAIL, "authoritative", 1.0,
                                 "HR roster row (authoritative root)", "hr_directory.csv"))
            by_email[e] = node.surrogate_id
        if row["slack_handle"]:
            node.add(Identifier("hr", "slack_handle", row["slack_handle"], TIER_SLACK_ID, "authoritative",
                                 1.0, "HR roster row (authoritative root)", "hr_directory.csv"))
            by_slack_id[row["slack_handle"]] = node.surrogate_id
        if row["github_login"]:
            node.add(Identifier("hr", "github_login", row["github_login"], TIER_GITHUB_LOGIN,
                                 "authoritative", 1.0, "HR roster row (authoritative root)",
                                 "hr_directory.csv"))
            by_github_login[row["github_login"]] = node.surrogate_id
        if row["display_name"]:
            node.add(Identifier("hr", "display_name", row["display_name"], TIER_NAME, "authoritative",
                                 1.0, "HR roster row (authoritative root)", "hr_directory.csv"))
            full = norm_name(row["display_name"])
            by_full_name.setdefault(full, []).append(node.surrogate_id)
            first = full.split(" ")[0].rstrip(".")
            by_first_name.setdefault(first, []).append(node.surrogate_id)

    # Precompiled once per run, not once per (message, name) pair -- scan_free_text() used to
    # re-`re.compile()` every unambiguous full name's pattern on every single free-text record it
    # scanned. That's pure waste at any data volume, independent of the still-open O(messages x
    # people) search-complexity question tracked in docs/architecture.md Sec 6 (this hoist doesn't
    # fix that -- it still runs `len(full_name_patterns)` searches per message -- it only removes
    # the redundant compilation on top of it).
    full_name_patterns = [
        (re.compile(r"\b" + re.escape(name).replace(r"\ ", r"\.?\s+") + r"\.?", re.IGNORECASE), sids[0])
        for name, sids in by_full_name.items() if len(sids) == 1
    ]

    indices = dict(by_emp_id=by_emp_id, by_email=by_email, by_slack_id=by_slack_id,
                   by_github_login=by_github_login, by_full_name=by_full_name,
                   by_first_name=by_first_name, full_name_patterns=full_name_patterns)
    return nodes, indices, registry


# --------------------------------------------------------------------------------------
# Pass 1 — deterministic structured-field matching (+ per-channel team-vote tally)
# --------------------------------------------------------------------------------------

def resolve_structured(nodes, idx, slack_msgs, jira_issues, github_prs, unresolved):
    channel_team_votes: dict[str, dict[str, int]] = {}

    for m in slack_msgs:
        sid = idx["by_slack_id"].get(m["user"])
        ref = f"slack:{m['channel']}:{m['ts']}"
        if sid:
            node = nodes[sid]
            node.add(Identifier("slack", "user_id", m["user"], TIER_SLACK_ID, "exact", 0.95,
                                 f"Slack `user` field matched HR slack_handle (channel={m['channel_name']})",
                                 ref))
            if node.team:
                votes = channel_team_votes.setdefault(m["channel_name"], {})
                votes[node.team] = votes.get(node.team, 0) + 1
        else:
            unresolved.append(UnresolvedMention("slack", ref, m["user"], "no_identifier_overlap",
                                                 "Slack user id not present in HR roster"))

    for issue in jira_issues:
        ref = f"jira:{issue['key']}:assignee"
        email = norm_email(issue.get("assignee_email", ""))
        sid = idx["by_email"].get(email)
        if sid:
            nodes[sid].add(Identifier("jira", "assignee_email", issue["assignee_email"], TIER_EMAIL,
                                       "exact", 0.95, f"Jira assignee_email field, issue {issue['key']}", ref))
        elif email:
            unresolved.append(UnresolvedMention("jira", ref, issue["assignee_email"],
                                                 "no_identifier_overlap", "assignee email not in HR roster"))

        for c_i, comment in enumerate(issue.get("comments", [])):
            ref_c = f"jira:{issue['key']}:comment[{c_i}]"
            author = comment["author"]
            if "@" in author:
                sid = idx["by_email"].get(norm_email(author))
                if sid:
                    nodes[sid].add(Identifier("jira", "comment_author_email", author, TIER_EMAIL, "exact",
                                               0.95, f"Jira comment author field, issue {issue['key']}", ref_c))
                    continue
            else:
                candidates = idx["by_full_name"].get(norm_name(author), [])
                if len(candidates) == 1:
                    nodes[candidates[0]].add(Identifier(
                        "jira", "comment_author_name", author, TIER_NAME, "exact", 0.80,
                        f"Jira comment author field is a full-name string, not verified against a "
                        f"directory schema (see requirements_analysis.md open question) — "
                        f"issue {issue['key']}", ref_c))
                    continue
            unresolved.append(UnresolvedMention("jira", ref_c, author, "ambiguous_or_unknown_author",
                                                 "comment author matched zero or >1 HR candidates"))

    for pr in github_prs:
        ref = f"github:pr#{pr['pr']}:author"
        m = PR_AUTHOR_RE.match(pr["author"])
        if m:
            sid = idx["by_email"].get(norm_email(m.group("email")))
            if sid:
                nodes[sid].add(Identifier("github", "pr_author", pr["author"], TIER_EMAIL, "exact", 0.95,
                                           f"GitHub PR author field, PR #{pr['pr']}", ref))
            else:
                unresolved.append(UnresolvedMention("github", ref, pr["author"], "no_identifier_overlap",
                                                      "PR author email not in HR roster"))
        for commit in pr.get("commits", []):
            ref_c = f"github:pr#{pr['pr']}:commit:{commit['sha']}"
            a = commit["author"]
            sid = None
            if "@" in a:
                sid = idx["by_email"].get(norm_email(a))
                ctype, tier = "commit_author_email", TIER_EMAIL
            else:
                sid = idx["by_github_login"].get(a)
                ctype, tier = "commit_author_login", TIER_GITHUB_LOGIN
            if sid:
                nodes[sid].add(Identifier("github", ctype, a, tier, "exact", 0.90,
                                           f"Commit {commit['sha']} author field, PR #{pr['pr']}", ref_c))
            else:
                unresolved.append(UnresolvedMention("github", ref_c, a, "no_identifier_overlap",
                                                      "commit author not in HR roster"))

    return channel_team_votes


# --------------------------------------------------------------------------------------
# Pass 2 — free-text scanning: emails, full names, then bare first names (contextual)
# --------------------------------------------------------------------------------------

def scan_free_text(nodes, idx, source, ref, text, channel_name, channel_team_votes, unresolved):
    consumed_spans = []

    # 2a. emails embedded in free text
    for m in EMAIL_RE.finditer(text):
        email = norm_email(m.group(0))
        sid = idx["by_email"].get(email)
        if sid:
            nodes[sid].add(Identifier(source, "free_text_email", m.group(0), TIER_EMAIL, "exact", 0.90,
                                       f"Email mentioned in free text: \"{text}\"", ref))
            consumed_spans.append(m.span())

    # 2b. unambiguous full-name mentions (exact match against a single HR display_name). Patterns
    # are precompiled once per run in build_nodes() -- see the comment there -- not recompiled
    # here per message.
    for pattern, sid in idx["full_name_patterns"]:
        m = pattern.search(text)
        if m:
            nodes[sid].add(Identifier(source, "free_text_full_name", m.group(0), TIER_NAME, "exact",
                                       0.75, f"Full name mentioned in free text: \"{text}\"", ref))
            consumed_spans.append(m.span())

    # 2c. bare first-name mentions not already covered by a full-name match above
    for word_m in WORD_RE.finditer(text):
        span = word_m.span()
        if any(span[0] >= s and span[1] <= e for s, e in consumed_spans):
            continue
        token = word_m.group(0)
        first = token.lower().rstrip(".")
        candidates = idx["by_first_name"].get(first)
        if not candidates or len(token) < 3:
            continue
        if len(candidates) == 1:
            nodes[candidates[0]].add(Identifier(source, "free_text_name_unique", token, TIER_NAME, "exact",
                                                  0.70, f"Unique-in-org first name mentioned: \"{text}\"", ref))
            continue
        # ambiguous across >1 candidate: only resolvable with channel team-affinity corroboration
        votes = channel_team_votes.get(channel_name, {})
        if votes:
            dominant_team, top = max(votes.items(), key=lambda kv: kv[1])
            matching = [sid for sid in candidates if nodes[sid].team == dominant_team]
            if len(matching) == 1:
                nodes[matching[0]].add(Identifier(
                    source, "bare_first_name_contextual", token, TIER_NAME, "contextual", 0.55,
                    f"Bare first name \"{token}\" resolved via channel team-affinity: channel "
                    f"'{channel_name}' dominated by team '{dominant_team}' ({top} structured-field "
                    f"matches), uniquely matching {nodes[matching[0]].display_name}. Message: \"{text}\"",
                    ref))
                continue
        unresolved.append(UnresolvedMention(
            source, ref, token, "ambiguous_multi_candidate",
            f"Bare first name \"{token}\" matches {len(candidates)} HR candidates "
            f"({[nodes[c].display_name for c in candidates]}) with no channel/team signal strong "
            f"enough to disambiguate. Per assignment instruction, not forcing a pick."))


def resolve_free_text(nodes, idx, slack_msgs, jira_issues, github_prs, channel_team_votes, unresolved):
    for m in slack_msgs:
        scan_free_text(nodes, idx, "slack", f"slack:{m['channel']}:{m['ts']}", m["text"],
                        m["channel_name"], channel_team_votes, unresolved)
    for issue in jira_issues:
        for c_i, comment in enumerate(issue.get("comments", [])):
            scan_free_text(nodes, idx, "jira", f"jira:{issue['key']}:comment[{c_i}]", comment["body"],
                            None, channel_team_votes, unresolved)
    for pr in github_prs:
        scan_free_text(nodes, idx, "github", f"github:pr#{pr['pr']}:title", pr["title"], None,
                        channel_team_votes, unresolved)
        for commit in pr.get("commits", []):
            scan_free_text(nodes, idx, "github", f"github:pr#{pr['pr']}:commit:{commit['sha']}",
                            commit["message"], None, channel_team_votes, unresolved)


# --------------------------------------------------------------------------------------
# Candidate-link detection (resemblance without a shared strong identifier — never auto-merged)
# --------------------------------------------------------------------------------------

def is_weakly_attested(node: PersonNode) -> bool:
    """A node is 'weak' if HR itself was missing one or more core fields for it. Used to gate
    resemblance checks so we don't flag two fully-attested, independently-verified people (e.g.
    two Priyas with distinct emails/slack ids/teams) just for sharing a first name."""
    core_types = {"emp_id", "work_email", "slack_handle", "github_login"}
    present = {i.type for i in node.identifiers if i.source == "hr"}
    return not core_types.issubset(present)


def name_tokens(display_name: str):
    parts = norm_name(display_name).split(" ")
    return parts[0], parts[-1]


def token_resembles(a: str, b: str) -> bool:
    """Exact match, or one side is an initial/abbreviation of the other (e.g. 'k' vs 'kumar')."""
    a, b = a.rstrip("."), b.rstrip(".")
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return False


def candidate_links(nodes):
    links = []
    for sid_a, sid_b in combinations(nodes.keys(), 2):
        a, b = nodes[sid_a], nodes[sid_b]
        if not (is_weakly_attested(a) or is_weakly_attested(b)):
            continue
        # Anchor signal: resemblance must be grounded in something surname-level or a shared
        # GitHub-login prefix. A bare shared first initial ("A." matches "Alice" just as much as
        # "Alex") is too weak to anchor a link on its own — it would fire against unrelated people
        # and drown the one real signal in noise. It's still recorded as a *supporting* reason once
        # an anchor is found.
        reasons = []
        first_a, last_a = name_tokens(a.display_name)
        first_b, last_b = name_tokens(b.display_name)
        gh_a = next((i.value for i in a.identifiers if i.type == "github_login" and i.source == "hr"), None)
        gh_b = next((i.value for i in b.identifiers if i.type == "github_login" and i.source == "hr"), None)

        anchor = False
        if token_resembles(last_a, last_b):
            reasons.append(f"surname/initial resemblance: '{last_a}' ~ '{last_b}'")
            anchor = True
        if gh_a and gh_b and gh_a.split("-")[0] == gh_b.split("-")[0]:
            reasons.append(f"shared GitHub login prefix: '{gh_a}' ~ '{gh_b}'")
            anchor = True
        if not anchor:
            continue

        if token_resembles(first_a, first_b):
            reasons.append(f"first-name/initial resemblance: '{first_a}' ~ '{first_b}'")
        if a.team and a.team == b.team:
            reasons.append(f"same HR team: '{a.team}'")
        confidence = round(min(0.25 + 0.15 * len(reasons), 0.6), 2)
        links.append({
            "node_a": sid_a, "node_a_name": a.display_name,
            "node_b": sid_b, "node_b_name": b.display_name,
            "reasons": reasons,
            "confidence": confidence,
            "status": "open",
            "note": "Resemblance only — no shared strong identifier (emp_id/email/slack/github) "
                    "exists between these two HR rows. NOT merged. Per FR-4, a merge requires either "
                    "a shared exact identifier or an explicit human-reviewed decision recorded here.",
        })
    return links


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def run(hr_rows=None, slack_msgs=None, jira_issues=None, github_prs=None, output_dir=None,
        registry_path=None) -> dict:
    """Run entity resolution end to end and return the ER mapping dict.

    Any of the four inputs can be passed directly (as already-parsed Python objects) for testing;
    omitted ones are loaded from sample_data/. If `output_dir` is given, er_mapping.json is written
    there; if None, nothing is written to disk (pure in-memory run, used by tests).

    `registry_path` controls surrogate-ID continuity across separate runs (see docs/architecture.md
    Sec 7.1 and assign_surrogate_id()): if given, it's loaded (or started fresh if it doesn't exist
    yet) and re-saved after this run, so a later run pointed at the same path picks up where this
    one left off. Defaults to `<output_dir>/identity_registry.json` when `output_dir` is given and
    `registry_path` isn't specified explicitly; with `output_dir=None` (the common in-memory test
    case), no registry is persisted and every row is treated as new -- which reproduces the exact
    sequential PERSON_0001, PERSON_0002, ... assignment the original position-based code always
    gave for a single, unchanging HR file.
    """
    hr_rows = load_hr() if hr_rows is None else hr_rows
    slack_msgs = load_json(paths.SLACK_JSON) if slack_msgs is None else slack_msgs
    jira_issues = load_json(paths.JIRA_JSON) if jira_issues is None else jira_issues
    github_prs = load_json(paths.GITHUB_JSON) if github_prs is None else github_prs

    if registry_path is None and output_dir is not None:
        registry_path = Path(output_dir) / "identity_registry.json"
    registry = load_registry(registry_path) if registry_path is not None else empty_registry()

    nodes, idx, registry = build_nodes(hr_rows, registry)
    unresolved: list[UnresolvedMention] = []

    channel_team_votes = resolve_structured(nodes, idx, slack_msgs, jira_issues, github_prs, unresolved)
    resolve_free_text(nodes, idx, slack_msgs, jira_issues, github_prs, channel_team_votes, unresolved)
    links = candidate_links(nodes)

    out = {
        "generated_by": "resolve_entities.py",
        "tier_legend": {
            "1": "emp_id (authoritative anchor)",
            "2": "email (work or raw, exact match)",
            "3": "platform user id (Slack user <-> HR slack_handle)",
            "4": "github_login (exact match)",
            "5": "name (full-name exact match, or bare-first-name only with channel/team corroboration)",
        },
        "people": [
            {
                "surrogate_id": sid,
                "display_name_for_review_only": node.display_name,  # NOT for the sellable product —
                                                                       # this crosswalk file itself is
                                                                       # the sensitive re-identification
                                                                       # key (NFR-5) and must be stored
                                                                       # separately from de-identified
                                                                       # output.
                "team": node.team,
                "identifiers": [asdict(i) for i in node.identifiers],
            }
            for sid, node in nodes.items()
        ],
        "candidate_links": links,
        "unresolved_mentions": [asdict(u) for u in unresolved],
        "summary": {
            "people_resolved": len(nodes),
            "candidate_links_open": len(links),
            "unresolved_mention_count": len(unresolved),
            "note": "In this sample, every raw mention ties deterministically or contextually to "
                    "some HR row (even the Slack-only 'Alex K.' contractor row resolves via its own "
                    "HR-assigned slack_handle). The genuine ambiguity in this dataset is at the "
                    "identity-MERGE level, not the mention level: see candidate_links for the "
                    "three-way 'Alex/Kumar' cluster that is deliberately left unmerged.",
        },
    }

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "er_mapping.json", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    if registry_path is not None:
        save_registry(registry, registry_path)

    return out


def main():
    out = run(output_dir=paths.OUTPUT)
    s = out["summary"]
    print(f"Wrote {paths.ER_MAPPING_JSON} — {s['people_resolved']} people, "
          f"{s['candidate_links_open']} open candidate links, {s['unresolved_mention_count']} "
          f"unresolved mentions")


if __name__ == "__main__":
    main()
