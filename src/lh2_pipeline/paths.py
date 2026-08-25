"""Shared path constants for the resolution / de-identification / assembly modules."""
from pathlib import Path

# src/lh2_pipeline/paths.py -> parents: [lh2_pipeline, src, <repo root>]
ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DATA = ROOT / "sample_data"
OUTPUT = ROOT / "output"
DEIDENT_DIR = OUTPUT / "deidentified"

HR_CSV = SAMPLE_DATA / "hr_directory.csv"
SLACK_JSON = SAMPLE_DATA / "slack.json"
JIRA_JSON = SAMPLE_DATA / "jira.json"
GITHUB_JSON = SAMPLE_DATA / "github.json"

ER_MAPPING_JSON = OUTPUT / "er_mapping.json"
IDENTITY_REGISTRY_JSON = OUTPUT / "identity_registry.json"
DECISION_UNIT_JSON = OUTPUT / "decision_unit_pay123.json"
DECISION_UNIT_AUDIT_JSON = OUTPUT / "decision_unit_pay123.internal_audit.json"

DEIDENT_SLACK_JSON = DEIDENT_DIR / "slack.json"
DEIDENT_JIRA_JSON = DEIDENT_DIR / "jira.json"
DEIDENT_GITHUB_JSON = DEIDENT_DIR / "github.json"
