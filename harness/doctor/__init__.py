"""V4 diagnose helpers: false-coverage, multi-tenant surface, soft-gate inventory."""

from __future__ import annotations

from harness.doctor.diagnose import (
    FalseCoverageReport,
    MultiTenantReport,
    SoftGateFinding,
    build_false_coverage_report,
    build_multi_tenant_report,
    inventory_soft_gates,
    render_false_coverage_markdown,
    render_multi_tenant_markdown,
    render_soft_gates_markdown,
)

__all__ = [
    "FalseCoverageReport",
    "MultiTenantReport",
    "SoftGateFinding",
    "build_false_coverage_report",
    "build_multi_tenant_report",
    "inventory_soft_gates",
    "render_false_coverage_markdown",
    "render_multi_tenant_markdown",
    "render_soft_gates_markdown",
]
