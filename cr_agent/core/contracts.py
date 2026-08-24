"""导出 JSON Schema 契约，用于跨组件数据校验。

为什么需要契约？
  - review_code 工具输出 findings，报告生成器读取 findings。
  - 如果工具输出格式变了，报告生成器就会出错。
  - JSON Schema 契约让格式显式化、可验证。
  - 生产环境：在 CI 中校验工具输出是否符合 schema。

用法：
    python -m cr_agent.core.contracts  # 导出所有 schema 到 contracts/
"""

from __future__ import annotations

import json
from pathlib import Path

from cr_agent.core.models import DiffMetrics, Finding, ReviewReport, StaticAnalysisResult

CONTRACTS_DIR = Path(__file__).parent.parent.parent / "contracts"


def export_schemas():
    """将所有 Pydantic 模型导出为 JSON Schema 文件。"""
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
