"""
In-memory CSV/JSON report bodies for the dashboard downloads.

The CLI writers in cost_analysis.py persist to a path; a browser download needs
the same bytes without touching disk, so this mirrors their exact column rules
(fieldnames come from the first record, JSON is indent=2) rather than inventing
a second format.
"""
import csv
import io
import json
from typing import Any, Dict, List


def to_csv(rows: List[Dict[str, Any]]) -> str:
    """Serialise report rows as CSV. Returns "" for no rows, like the CLI writer."""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def to_json(payload: Dict[str, Any]) -> str:
    """Serialise the whole dashboard payload, matching the CLI's indent=2 output."""
    return json.dumps(payload, indent=2)
