"""Export JSON Schema contracts for cross-component data validation.

Why contracts?
  - The review_code tool outputs findings. The report generator reads findings.
  - If the tool changes its output format, the report generator breaks.
  - A JSON Schema contract makes the format explicit and verifiable.
  - Production: use in CI to validate that tool output matches schema.

Usage:
    python -m cr_agent.core.contracts  # Export all schemas to contracts/
"""

from __future__ import annotations

import json
from pathlib import Path

from cr_agent.core.models import Finding, ReviewReport, DiffMetrics, StaticAnalysisResult

CONTRACTS_DIR = Path(__file__).parent.parent.parent / "contracts"


def export_schemas():
    """Export all Pydantic models as JSON Schema files."""
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)

    schemas = {
        "finding.v1.schema.json": Finding.model_json_schema(),
        "review_report.v1.schema.json": ReviewReport.model_json_schema(),
        "diff_metrics.v1.schema.json": DiffMetrics.model_json_schema(),
        "static_analysis.v1.schema.json": StaticAnalysisResult.model_json_schema(),
    }

    for filename, schema in schemas.items():
        path = CONTRACTS_DIR / filename
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
        print(f"Exported: {path}")


if __name__ == "__main__":
    export_schemas()
