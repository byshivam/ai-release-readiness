import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from readiness.assess import ACCEPTED, GAP, MITIGATED, OPEN, assess, decide, rating_for
from readiness.cli import main
from readiness.evidence import build_evidence

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


@pytest.fixture
def cfg():
    return {
        "system": load("systems/banking-rag-eval.yaml"),
        "register": load("risk_register.yaml"),
        "policy": load("policy.yaml"),
        "nist": load("nist_ai_rmf.yaml"),
    }


def write_run(folder: Path, when: str, summary: dict, *, model="openai/gpt-oss-20b", prompt="v2", reasons=()):
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    data = {"run_at": when, "generator_model": model, "prompt_version": prompt, "judge_model": "judge",
            "decision": "GO", "reasons": list(reasons), "summary": summary, "n_cases": 24}
    (folder / "runs" / f"{when.replace(' ', '-').replace(':', '')}.json").write_text(json.dumps(data))


GOOD = {"retrieval_hit_rate": 1.0, "fact_accuracy": 1.0, "refusal_accuracy": 1.0, "false_refusal_rate": 0.0,
        "citation_validity": 0.95, "safety_pass_rate": 1.0, "faithfulness": 0.9, "answer_relevancy": 0.9,
        "contextual_recall": 0.8}


def run(cfg, folder, as_of=AS_OF):
    ev = build_evidence(folder, cfg["system"], cfg["policy"], as_of)
    return assess(cfg["register"], ev, cfg["policy"])


def by_id(result):
    return {r.id: r for r in result.risks}


def test_real_reports_give_conditional_go(cfg):
    result = run(cfg, FIXTURES)
    risks = by_id(result)
    assert result.decision == "CONDITIONAL GO"
    assert risks["R10"].status == ACCEPTED
    # Faithfulness was only measured in the second (incomplete) run — it must still count.
    assert risks["R01"].status == MITIGATED


def test_all_mitigated_is_go_except_accepted(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD)
    write_run(tmp_path, "2026-10-07 02:00 UTC", GOOD)
    cfg["register"]["risks"] = [r for r in cfg["register"]["risks"] if r["id"] != "R10"]
    assert run(cfg, tmp_path).decision == "GO"


def test_failing_critical_metric_blocks_release(cfg, tmp_path):
    write_run(tmp_path, "2026-10-07 02:00 UTC", {**GOOD, "safety_pass_rate": 0.67})
    result = run(cfg, tmp_path)
    assert by_id(result)["R06"].status == OPEN
    assert result.decision == "NO-GO"


def test_regression_between_runs_is_caught(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD)
    write_run(tmp_path, "2026-10-07 02:00 UTC", {**GOOD, "citation_validity": 0.91, "fact_accuracy": 0.85})
    result = run(cfg, tmp_path)
    r08 = by_id(result)["R08"]
    assert r08.status == OPEN
    assert "fact_accuracy" in r08.checks[0].detail
    assert result.decision == "NO-GO"  # R08 is rated high


def test_stale_evidence_becomes_a_gap(cfg, tmp_path):
    write_run(tmp_path, "2026-09-01 02:00 UTC", GOOD)
    result = run(cfg, tmp_path)
    assert by_id(result)["R02"].status == GAP
    assert result.decision == "NO-GO"  # R02 is critical with no current evidence


def test_evidence_from_another_model_does_not_count(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD, model="llama-3.1-8b-instant")
    write_run(tmp_path, "2026-10-07 02:00 UTC", GOOD, model="llama-3.1-8b-instant")
    result = run(cfg, tmp_path)
    risks = by_id(result)
    assert risks["R09"].status == OPEN  # evaluated model differs from the approved one
    assert risks["R01"].status == GAP


def test_missing_supporting_evidence_only_adds_a_note(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", {k: v for k, v in GOOD.items() if k != "contextual_recall"})
    write_run(tmp_path, "2026-10-07 02:00 UTC", {k: v for k, v in GOOD.items() if k != "contextual_recall"})
    r07 = by_id(run(cfg, tmp_path))["R07"]
    assert r07.status == MITIGATED
    assert any("contextual_recall" in n for n in r07.notes)


def test_rating_bands(cfg):
    policy = cfg["policy"]
    assert [rating_for(s, policy) for s in (1, 5, 10, 15, 25)] == ["low", "medium", "high", "critical", "critical"]


def test_decide_with_no_risks_open():
    assert decide([])[0] == "GO"


def test_every_nist_reference_is_defined(cfg):
    defined = {sub for subs in cfg["nist"].values() for sub in subs}
    used = {sub for r in cfg["register"]["risks"] for sub in r["nist"]}
    assert used <= defined


def test_cli_writes_all_outputs(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "README.md").write_text("intro\n<!-- READINESS:START -->\nold\n<!-- READINESS:END -->\n")
    code = main(["--evidence", str(FIXTURES), "--root", str(tmp_path), "--as-of", "2026-10-07 08:00"])
    assert code == 0
    for name in ("MODEL_CARD.md", "RISK_REGISTER.md", "NIST_AI_RMF.md", "RELEASE_DECISION.md"):
        assert (tmp_path / "reports" / name).read_text().startswith("<!-- Generated")
    html = (tmp_path / "docs" / "index.html").read_text()
    assert "CONDITIONAL GO" in html and "<script" not in html
    readme = (tmp_path / "README.md").read_text()
    assert "old" not in readme and "CONDITIONAL GO" in readme
