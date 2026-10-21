"""Issue #2052: per-phase report of ONLY the metrics an
``evidence_index.EvidenceIndex`` consumer can actually observe:
``fetch_count`` / ``emitted_utf8_bytes`` / ``snapshot_reuse_count`` /
``duplicate_projection_count``.

This module never fabricates a token count or a model-turn count. Neither
figure is observable from this layer (an ``EvidenceIndex`` only sees
already-fetched Issue/comment payload text, never the model's own tokenizer
or turn boundaries), so this report simply does not have those fields --
callers must not synthesize them from byte counts or any other proxy.

Out of scope (Issue #2052 Out of Scope): the output shape here is
intentionally recommended-but-not-forced to line up with ``#1117``'s
``CONTEXT_BUDGET_DECISION_V1`` family (both report "how much was consumed"),
but the two are independent implementations for independent scopes --
static docs/skill/hooks/rules/scripts text budgeting (#1093/#1117) vs.
runtime GitHub Issue/comment evidence reuse (this Issue). This module does
not import, subclass, or otherwise couple to anything under `#1117`'s
ownership.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CONTEXT_BUDGET_REPORT_SCHEMA_VERSION",
    "OBSERVED_METRIC_FIELDS",
    "ContextBudgetReport",
]

CONTEXT_BUDGET_REPORT_SCHEMA_VERSION = "context_budget_report/v1"

# The CLOSED set of metric fields this report ever emits per phase. Any
# extension must add a new named, still-actually-observed field here --
# never a token/model-turn estimate.
OBSERVED_METRIC_FIELDS = (
    "fetch_count",
    "emitted_utf8_bytes",
    "snapshot_reuse_count",
    "duplicate_projection_count",
)


def _zero_metrics() -> dict:
    return {field_name: 0 for field_name in OBSERVED_METRIC_FIELDS}


@dataclass
class ContextBudgetReport:
    """Accumulates observed-only per-phase metrics for a single
    ``run_refinement_preflight.py`` invocation (or any other
    ``EvidenceIndex`` consumer) and renders them as
    ``CONTEXT_BUDGET_REPORT_V1``.
    """

    consumer: str
    _phases: "dict[str, dict]" = field(default_factory=dict)
    _phase_order: "list[str]" = field(default_factory=list)

    def record_phase(self, phase: str, metrics: dict) -> None:
        """Record (or overwrite) the observed metrics for ``phase``.
        ``metrics`` must only contain keys from ``OBSERVED_METRIC_FIELDS``
        (extra/unknown keys are rejected fail-closed; this is what keeps an
        unobserved token/model-turn estimate from silently entering the
        report)."""
        unknown = set(metrics) - set(OBSERVED_METRIC_FIELDS)
        if unknown:
            raise ValueError(
                f"context_budget_report: refusing to record un-observed/unknown metric "
                f"field(s) {sorted(unknown)} for phase {phase!r} -- only "
                f"{OBSERVED_METRIC_FIELDS} are accepted"
            )
        normalized = _zero_metrics()
        for key, value in metrics.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"context_budget_report: metric {key!r} for phase {phase!r} must be an "
                    f"int (observed count/byte-size), got {type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"context_budget_report: metric {key!r} for phase {phase!r} must be >= 0, "
                    f"got {value}"
                )
            normalized[key] = value
        if phase not in self._phases:
            self._phase_order.append(phase)
        self._phases[phase] = normalized

    def record_from_evidence_index(self, phase: str, evidence_index) -> None:
        """Convenience wrapper: record ``evidence_index.metrics_snapshot()``
        (an ``evidence_index.EvidenceIndex`` instance) for ``phase``."""
        self.record_phase(phase, evidence_index.metrics_snapshot())

    def phases(self) -> "list[str]":
        return list(self._phase_order)

    def totals(self) -> dict:
        """Sum of each observed metric across all recorded phases."""
        totals = _zero_metrics()
        for metrics in self._phases.values():
            for key in OBSERVED_METRIC_FIELDS:
                totals[key] += metrics[key]
        return totals

    def to_dict(self) -> dict:
        return {
            "schema": "CONTEXT_BUDGET_REPORT_V1",
            "schema_version": CONTEXT_BUDGET_REPORT_SCHEMA_VERSION,
            "consumer": self.consumer,
            "phases": {phase: dict(self._phases[phase]) for phase in self._phase_order},
            "totals": self.totals(),
        }

    def write_json(self, path: "Path | str") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def from_dict(cls, payload: dict) -> "ContextBudgetReport":
        if payload.get("schema") != "CONTEXT_BUDGET_REPORT_V1":
            raise ValueError("context_budget_report: not a CONTEXT_BUDGET_REPORT_V1 payload")
        report = cls(consumer=str(payload.get("consumer", "")))
        for phase, metrics in (payload.get("phases") or {}).items():
            report.record_phase(phase, metrics)
        return report
