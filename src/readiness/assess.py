"""Score each risk against its evidence and reach a release recommendation."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field

from readiness.evidence import EvidenceSet

OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
RATING_ORDER = ["low", "medium", "high", "critical"]

MITIGATED, OPEN, GAP, ACCEPTED = "Mitigated", "Open", "Evidence gap", "Accepted"


@dataclass
class CheckResult:
    label: str
    outcome: str  # pass | fail | missing | stale
    required: bool
    detail: str


@dataclass
class RiskResult:
    risk: dict
    inherent_score: int
    inherent_rating: str
    status: str
    residual_rating: str
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.risk["id"]


@dataclass
class Assessment:
    risks: list[RiskResult]
    decision: str
    reasons: list[str]
    evidence: EvidenceSet


def rating_for(score: int, policy: dict) -> str:
    thresholds = policy["scoring"]["ratings"]
    for name in reversed(RATING_ORDER):
        if score >= thresholds[name]:
            return name
    return "low"


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _check_metric(spec: dict, evidence: EvidenceSet) -> CheckResult:
    ev = evidence.metric(spec["metric"])
    required = spec.get("required", True)
    label = f"{spec['metric']} {spec['op']} {spec['threshold']:.2f}"
    if ev.status == "missing":
        return CheckResult(label, "missing", required, ev.note)
    passed = OPS[spec["op"]](ev.value, spec["threshold"])
    detail = f"{_fmt(ev.value)} on {ev.run.run_at:%Y-%m-%d %H:%M} UTC"
    if ev.status == "stale":
        return CheckResult(label, "stale", required, f"{detail} ({ev.note})")
    return CheckResult(label, "pass" if passed else "fail", required, detail)


def _check_no_regression(spec: dict, evidence: EvidenceSet, risk_specs: list[dict]) -> CheckResult:
    tolerance = spec.get("tolerance", 0.05)
    label = f"no metric drops more than {tolerance:.2f} between comparable runs"
    tracked = sorted({e["metric"]: e for r in risk_specs for e in r.get("evidence", []) if e.get("type") == "metric"}.values(), key=lambda e: e["metric"])
    compared, drops = 0, []
    for e in tracked:
        series = evidence.series(e["metric"])
        if len(series) < 2:
            continue
        compared += 1
        (_, prev), (_, last) = series[-2], series[-1]
        lower_is_better = e["op"] in ("<=", "<")
        drop = (last - prev) if lower_is_better else (prev - last)
        if drop > tolerance:
            drops.append(f"{e['metric']} {_fmt(prev)} → {_fmt(last)}")
    if compared == 0:
        return CheckResult(label, "missing", True, "fewer than two comparable runs for every metric")
    if drops:
        return CheckResult(label, "fail", True, "; ".join(drops))
    return CheckResult(label, "pass", True, f"{compared} metrics compared across the last two runs")


def _check_config(evidence: EvidenceSet) -> CheckResult:
    label = "evaluated configuration equals approved configuration"
    run = evidence.latest_run
    approved = evidence.approved
    if run is None:
        return CheckResult(label, "missing", True, "no evaluation runs found")
    if run.matches(approved):
        return CheckResult(label, "pass", True, f"{run.generator_model} · prompt {run.prompt_version}")
    return CheckResult(
        label,
        "fail",
        True,
        f"latest run used {run.generator_model} · prompt {run.prompt_version}; approved is "
        f"{approved.get('generator_model')} · prompt {approved.get('prompt_version')}",
    )


def assess_risk(risk: dict, evidence: EvidenceSet, policy: dict, all_risks: list[dict]) -> RiskResult:
    score = int(risk["likelihood"]) * int(risk["impact"])
    inherent = rating_for(score, policy)
    checks = []
    for spec in risk.get("evidence", []):
        kind = spec.get("type", "metric")
        if kind == "metric":
            checks.append(_check_metric(spec, evidence))
        elif kind == "no_regression":
            checks.append(_check_no_regression(spec, evidence, all_risks))
        elif kind == "config_match":
            checks.append(_check_config(evidence))
        else:
            raise ValueError(f"{risk['id']}: unknown evidence type {kind!r}")

    required = [c for c in checks if c.required]
    notes = [f"supporting evidence {c.outcome}: {c.label} ({c.detail})" for c in checks if not c.required and c.outcome != "pass"]

    if any(c.outcome == "fail" for c in required):
        status = OPEN
    elif required and all(c.outcome == "pass" for c in required):
        status = MITIGATED
    elif risk.get("acceptance"):
        status = ACCEPTED
    else:
        status = GAP

    if status == MITIGATED:
        # Controls are evidenced: residual likelihood drops to rare, impact is unchanged.
        residual = rating_for(int(risk["impact"]), policy)
    else:
        residual = inherent
    return RiskResult(risk, score, inherent, status, residual, checks, notes)


def decide(results: list[RiskResult]) -> tuple[str, list[str]]:
    severe = {"high", "critical"}
    reasons: list[str] = []
    open_severe = [r for r in results if r.status == OPEN and r.inherent_rating in severe]
    if open_severe:
        reasons = [f"{r.id} is open and rated {r.inherent_rating}: {r.risk['title']}" for r in open_severe]
        return "NO-GO", reasons
    critical_gaps = [r for r in results if r.status == GAP and r.inherent_rating == "critical"]
    if critical_gaps:
        reasons = [f"{r.id} is rated critical and has no current evidence: {r.risk['title']}" for r in critical_gaps]
        return "NO-GO", reasons
    conditional = [r for r in results if r.status != MITIGATED]
    if conditional:
        reasons = [f"{r.id} — {r.status.lower()}: {r.risk['title']}" for r in conditional]
        return "CONDITIONAL GO", reasons
    return "GO", ["Every risk is mitigated by current evidence"]


def assess(register: dict, evidence: EvidenceSet, policy: dict) -> Assessment:
    risks = register["risks"]
    results = [assess_risk(r, evidence, policy, risks) for r in risks]
    decision, reasons = decide(results)
    return Assessment(results, decision, reasons, evidence)
