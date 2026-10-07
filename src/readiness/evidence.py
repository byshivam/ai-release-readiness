"""Load evaluation runs and pick the evidence that is allowed to count.

A run report is the JSON written by an evaluation suite (for example
banking-rag-eval's `evals/run_eval.py`). One run may be incomplete — the LLM judge
can stop part-way when a free-tier quota runs out — so evidence is collected per
metric: each metric's value comes from the newest run that

  * actually measured it,
  * used the approved model and prompt (when the policy requires it), and
  * is not older than the policy's maximum evidence age.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Run:
    run_at: datetime
    generator_model: str
    prompt_version: str
    judge_model: str | None
    decision: str
    reasons: list[str]
    summary: dict[str, float | None]
    n_cases: int
    source: str

    @property
    def complete(self) -> bool:
        return not any(r.startswith("evaluation incomplete") for r in self.reasons)

    def matches(self, config: dict) -> bool:
        return (
            self.generator_model == config.get("generator_model")
            and self.prompt_version == config.get("prompt_version")
        )


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
    _cache: dict[str, MetricEvidence] = field(default_factory=dict)

    @property
    def eligible_runs(self) -> list[Run]:
        if not self.require_match:
            return list(self.runs)
        return [r for r in self.runs if r.matches(self.approved)]

    @property
    def latest_run(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def metric(self, name: str) -> MetricEvidence:
        if name in self._cache:
            return self._cache[name]
        found = None
        for run in reversed(self.eligible_runs):
            if run.summary.get(name) is not None:
                found = run
                break
        if found is None:
            ev = MetricEvidence(name, None, None, "missing", "not measured by any run of the approved configuration")
        elif self.as_of - found.run_at > self.max_age:
            age = (self.as_of - found.run_at).days
            ev = MetricEvidence(name, found.summary[name], found, "stale", f"last measured {age} days ago")
        else:
            ev = MetricEvidence(name, found.summary[name], found, "current")
        self._cache[name] = ev
        return ev

    def series(self, name: str) -> list[tuple[datetime, float]]:
        return [(r.run_at, r.summary[name]) for r in self.eligible_runs if r.summary.get(name) is not None]


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
        generator_model=data.get("generator_model", ""),
        prompt_version=data.get("prompt_version", ""),
        judge_model=data.get("judge_model"),
        decision=data.get("decision", ""),
        reasons=list(data.get("reasons", [])),
        summary=dict(data.get("summary", {})),
        n_cases=int(data.get("n_cases", 0)),
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
    )
