"""Generate the governance pack for every AI system in config/systems/.

Usage:
    python -m readiness.cli                                  # all systems, evidence in evidence/<system-id>/
    python -m readiness.cli --system hr-agent-eval           # one system
    python -m readiness.cli --evidence-root path/to/evidence
    python -m readiness.cli --as-of "2026-10-07 12:00"       # reproducible runs
    python -m readiness.cli --strict                         # exit 1 if any system is NO-GO

For each system: reports/<id>/*.md and docs/<id>.html. Across systems: docs/index.html
and the README status block.
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
    p.add_argument("--system", action="append", help="system id (repeatable); default: every system in config/systems/")
    p.add_argument("--evidence-root", type=Path, help="folder holding one evidence folder per system (default: <root>/evidence)")
    p.add_argument("--root", type=Path, default=ROOT, help="project root (config/ in, reports/ docs/ README out)")
    p.add_argument("--as-of", help='"YYYY-MM-DD HH:MM" in UTC; defaults to now')
    p.add_argument("--strict", action="store_true", help="exit with status 1 when any system is NO-GO")
    args = p.parse_args(argv)

    cfg = args.root / "config"
    evidence_root = args.evidence_root or args.root / "evidence"
    policy = load_yaml(cfg / "policy.yaml")
    nist = load_yaml(cfg / "nist_ai_rmf.yaml")
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) if args.as_of else datetime.now(timezone.utc)
    ids = args.system or sorted(path.stem for path in (cfg / "systems").glob("*.yaml"))

    entries = []
    for sid in ids:
        system = load_yaml(cfg / "systems" / f"{sid}.yaml")
        register = load_yaml(cfg / "risks" / f"{sid}.yaml")
        evidence = build_evidence(evidence_root / sid, system, policy, as_of)
        if not evidence.runs:
            print(f"{sid}: no evaluation runs found in {evidence_root / sid}", file=sys.stderr)
            return 2
        result = assess(register, evidence, policy)
        entries.append((system, result))

        out = args.root / "reports" / sid
        out.mkdir(parents=True, exist_ok=True)
        (out / "MODEL_CARD.md").write_text(render.model_card(system, result), encoding="utf-8")
        (out / "RISK_REGISTER.md").write_text(render.risk_register(system, result), encoding="utf-8")
        (out / "NIST_AI_RMF.md").write_text(render.nist_mapping(nist, register, result), encoding="utf-8")
        (out / "RELEASE_DECISION.md").write_text(render.release_decision(system, result), encoding="utf-8")
        docs = args.root / "docs"
        docs.mkdir(exist_ok=True)
        (docs / f"{sid}.html").write_text(dashboard.render(system, register, nist, result), encoding="utf-8")

        print(f"{sid}: {result.decision}")
        for reason in result.reasons:
            print(f"  - {reason}")
        for r in result.risks:
            print(f"    {r.id} {r.status:<13} inherent {r.inherent_rating:<8} residual {r.residual_rating}")

    (args.root / "docs" / "index.html").write_text(dashboard.render_overview(entries), encoding="utf-8")
    readme = args.root / "README.md"
    if readme.exists() and render.START in readme.read_text(encoding="utf-8"):
        readme.write_text(render.update_readme(readme.read_text(encoding="utf-8"), render.readme_block(entries)), encoding="utf-8")

    if args.strict and any(a.decision == "NO-GO" for _, a in entries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
