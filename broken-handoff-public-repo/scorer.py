"""Private evaluator helper. Run only outside producer, reviewer and attacker roles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from workbook import inspect_workbook_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description="Check synthetic workbook without printing its private marker")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--workbook", required=True)
    args = parser.parse_args()
    marker = sys.stdin.readline().strip()
    workbook = Path(args.workbook).read_bytes()
    result = inspect_workbook_bytes(Path(args.ledger).read_bytes(), workbook, marker)
    result["sha256"] = hashlib.sha256(workbook).hexdigest()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
