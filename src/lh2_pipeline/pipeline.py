#!/usr/bin/env python3
"""Run the full pipeline in order: resolve -> de-identify -> assemble.

`poetry run run-pipeline` regenerates everything under output/ from sample_data/ in one shot.
Equivalent to running the three scripts individually; see README.md for why you might want to run
them individually instead (e.g. to inspect er_mapping.json before de-identifying).
"""
from . import assemble_decision_unit, deidentify, resolve_entities


def main():
    resolve_entities.main()
    deidentify.main()
    assemble_decision_unit.main()


if __name__ == "__main__":
    main()
