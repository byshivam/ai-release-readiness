"""Load evaluation runs and pick the evidence that is allowed to count.

A run report is the JSON written by an evaluation suite (banking-rag-eval and
hr-agent-eval use the same shape). Free-tier API limits can stop a run part-way,
in two different ways, and they are treated differently:

* **The run stopped before every test case ran** ("stopped after 11 of 25").
  Every metric in that run describes only part of the test set, so the whole run
  is excluded from evidence. It still counts as "the latest run" for checking
  which model was evaluated.
* **Only the LLM judge stopped** ("LLM judge stopped at …"). Deterministic metrics
  cover every case and still count; the system's judge metrics from that run
  cover only some cases and are excluded.

For each metric, evidence is the newest value from a run that measured it fully,
used the approved model and prompt (when the policy requires it), and is not older
than the policy's maximum evidence age.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PARTIAL_COVERAGE = re.compile(r"stopped after \d+ of \d+")
JUDGE_STOPPED = re.compile(r"judge stopped", re.IGNORECASE)


@dataclass(frozen=True)
class Run:
    run_at: datetime
    model: str
    prompt_version: str
    judge_model: str | None
    decision: str
    reasons: list[str]
    summary: dict[str, float | None]
    n_cases: int
    source: str

    @property
    def partial_coverage(self) -> bool:
        return any(PARTIAL_COVERAGE.search(r) for r in self.reasons)

    @property
    def judge_incomplete(self) -> bool:
        return any(JUDGE_STOPPED.search(r) for r in self.reasons)

    @property
    def complete(self) -> bool:
        return not (self.partial_coverage or self.judge_incomplete)

    def matches(self, config: dict) -> bool:
        return self.model == config.get("model") and self.prompt_version == config.get("prompt_version")


@dataclass(frozen=True)
class MetricEvidence:
    metric: str
    value: float | None
    run: Run | None
    status: str  # current | stale | missing
    note: str = ""


@dataclass
class EvidenceSet:
    runs: list[Run]
    approved: dict
    as_of: datetime
    max_age: timedelta
    require_match: bool
    judge_metrics: frozenset[str] = frozenset()
    _cache: dict[str, MetricEvidence] = field(default_factory=dict)

    @property
    def eligible_runs(self) -> list[Run]:
        """Runs that ran the full test set on the approved configuration."""
        runs = [r for r in self.runs if not r.partial_coverage]
        if self.require_match:
            runs = [r for r in runs if r.matches(self.approved)]
        return runs

    @property
    def excluded_runs(self) -> list[Run]:
        eligible = set(id(r) for r in self.eligible_runs)
        return [r for r in self.runs if id(r) not in eligible]

    @property
    def latest_run(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def _usable(self, run: Run, name: str) -> bool:
        if run.summary.get(name) is None:
            return False
        return not (name in self.judge_metrics and run.judge_incomplete)

    def metric(self, name: str) -> MetricEvidence:
        if name in self._cache:
            return self._cache[name]
        found = next((r for r in reversed(self.eligible_runs) if self._usable(r, name)), None)
        if found is None:
            note = "no run of the approved configuration measured it on the full test set"
            ev = MetricEvidence(name, None, None, "missing", note)
        elif self.as_of - found.run_at > self.max_age:
            age = (self.as_of - found.run_at).days
            ev = MetricEvidence(name, found.summary[name], found, "stale", f"last fully measured {age} days ago")
        else:
            ev = MetricEvidence(name, found.summary[name], found, "current")
        self._cache[name] = ev
        return ev

    def series(self, name: str) -> list[tuple[datetime, float]]:
        return [(r.run_at, r.summary[name]) for r in self.eligible_runs if self._usable(r, name)]


def _parse_time(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised run_at timestamp: {text!r}")


def load_run(path: Path) -> Run:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Run(
        run_at=_parse_time(data["run_at"]),
        model=data.get("generator_model") or data.get("agent_model") or data.get("model") or "",
        prompt_version=data.get("prompt_version", ""),
        judge_model=data.get("judge_model"),
        decision=data.get("decision", ""),
        reasons=list(data.get("reasons", [])),
        summary=dict(data.get("summary", {})),
        n_cases=int(data.get("n_cases") or data.get("n_scenarios") or 0),
        source=path.name,
    )


def load_runs(evidence_dir: Path) -> list[Run]:
    """Load every run report under `evidence_dir/runs`, oldest first, de-duplicated by timestamp."""
    paths = sorted((evidence_dir / "runs").glob("*.json"))
    if not paths and (evidence_dir / "latest.json").exists():
        paths = [evidence_dir / "latest.json"]
    runs: dict[datetime, Run] = {}
    for path in paths:
        run = load_run(path)
        runs[run.run_at] = run
    return [runs[k] for k in sorted(runs)]


def build_evidence(evidence_dir: Path, system: dict, policy: dict, as_of: datetime | None = None) -> EvidenceSet:
    return EvidenceSet(
        runs=load_runs(evidence_dir),
        approved=system.get("approved_configuration", {}),
        as_of=as_of or datetime.now(timezone.utc),
        max_age=timedelta(days=policy["evidence"]["max_age_days"]),
        require_match=policy["evidence"].get("require_matching_configuration", True),
        judge_metrics=frozenset(system.get("judge_metrics", [])),
    )
