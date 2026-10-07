"""Generate every readiness artifact for one system.

Usage:
    python -m readiness.cli --system banking-rag-eval --evidence evidence/banking-rag-eval
    python -m readiness.cli ... --as-of "2026-10-07 12:00"      # reproducible runs
    python -m readiness.cli ... --strict                        # exit 1 on NO-GO

Writes reports/*.md, docs/index.html and the README status block.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from readiness import dashboard, render
from readiness.assess import assess
from readiness.evidence import build_evidence

ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--system", default="banking-rag-eval")
    p.add_argument("--evidence", type=Path, required=True, help="folder holding runs/*.json from the eval suite")
    p.add_argument("--root", type=Path, default=ROOT, help="project root (config/ in, reports/ docs/ README out)")
    p.add_argument("--as-of", help='"YYYY-MM-DD HH:MM" in UTC; defaults to now')
    p.add_argument("--strict", action="store_true", help="exit with status 1 when the recommendation is NO-GO")
    args = p.parse_args(argv)

    cfg = args.root / "config"
    system = load_yaml(cfg / "systems" / f"{args.system}.yaml")
    register = load_yaml(cfg / "risk_register.yaml")
    policy = load_yaml(cfg / "policy.yaml")
    nist = load_yaml(cfg / "nist_ai_rmf.yaml")
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) if args.as_of else None

    evidence = build_evidence(args.evidence, system, policy, as_of)
    if not evidence.runs:
        print(f"No evaluation runs found in {args.evidence}", file=sys.stderr)
        return 2
    result = assess(register, evidence, policy)

    reports = args.root / "reports"
    docs = args.root / "docs"
    reports.mkdir(exist_ok=True)
    docs.mkdir(exist_ok=True)
    (reports / "MODEL_CARD.md").write_text(render.model_card(system, result), encoding="utf-8")
    (reports / "RISK_REGISTER.md").write_text(render.risk_register(system, result), encoding="utf-8")
    (reports / "NIST_AI_RMF.md").write_text(render.nist_mapping(nist, register, result), encoding="utf-8")
    (reports / "RELEASE_DECISION.md").write_text(render.release_decision(system, result), encoding="utf-8")
    (docs / "index.html").write_text(dashboard.render(system, register, nist, result), encoding="utf-8")
    readme = args.root / "README.md"
    if readme.exists() and render.START in readme.read_text(encoding="utf-8"):
        readme.write_text(render.update_readme(readme.read_text(encoding="utf-8"), render.readme_block(system, result)), encoding="utf-8")

    print(f"Recommendation: {result.decision}")
    for reason in result.reasons:
        print(f"  - {reason}")
    for r in result.risks:
        print(f"  {r.id} {r.status:<13} inherent {r.inherent_rating:<8} residual {r.residual_rating}")
    if args.strict and result.decision == "NO-GO":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
