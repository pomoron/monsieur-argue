#!/usr/bin/env python3
"""Root entry point for the Legora negotiation assessor CLI.

Usage:
    python assess.py --transcript <t.json> --scenario <s.json> [--playbook playbook.json]

See README.md for the full option list and the JSON contract.
"""

from assessor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
