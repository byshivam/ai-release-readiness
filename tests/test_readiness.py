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
SYSTEMS = ["banking-rag-eval", "hr-agent-eval"]


def load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


def cfg_for(sid):
    return {
        "system": load(f"systems/{sid}.yaml"),
        "register": load(f"risks/{sid}.yaml"),
        "policy": load("policy.yaml"),
        "nist": load("nist_ai_rmf.yaml"),
    }


@pytest.fixture
def cfg():
    return cfg_for("banking-rag-eval")


def write_run(folder: Path, when: str, summary: dict, *, model="openai/gpt-oss-20b", prompt="v2", reasons=()):
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    data = {"run_at": when, "generator_model": model, "prompt_version": prompt, "judge_model": "judge",
            "decision": "GO", "reasons": list(reasons), "summary": summary, "n_cases": 24}
    (folder / "runs" / f"{when.replace(' ', '-').replace(':', '')}.json").write_text(json.dumps(data))


GOOD = {"retrieval_hit_rate": 1.0, "fact_accuracy": 1.0, "refusal_accuracy": 1.0, "false_refusal_rate": 0.0,
        "citation_validity": 0.95, "safety_pass_rate": 1.0, "faithfulness": 0.9, "answer_relevancy": 0.9,
        "contextual_recall": 0.8}
JUDGE_STOPPED = "evaluation incomplete — LLM judge stopped at case fact-03: Groq daily limit reached"
PARTIAL = "evaluation incomplete — stopped after 11 of 25 scenarios: Groq daily limit reached"


def run(cfg, folder, as_of=AS_OF):
    ev = build_evidence(folder, cfg["system"], cfg["policy"], as_of)
    return assess(cfg["register"], ev, cfg["policy"])


def by_id(result):
    return {r.id: r for r in result.risks}


# ----------------------------------------------------------------- real evaluation history

def test_banking_real_runs_block_release_until_faithfulness_is_fully_judged():
    result = run(cfg_for("banking-rag-eval"), FIXTURES / "banking-rag-eval")
    risks = by_id(result)
    # The judge stopped part-way in both real runs, so faithfulness was never measured on the full set.
    assert risks["R01"].status == GAP
    assert result.decision == "NO-GO"
    # Deterministic metrics from those same runs still count.
    assert risks["R02"].status == MITIGATED and risks["R06"].status == MITIGATED
    assert risks["R10"].status == ACCEPTED


def test_hr_real_runs_ignore_the_partial_run():
    cfg = cfg_for("hr-agent-eval")
    result = run(cfg, FIXTURES / "hr-agent-eval")
    risks = by_id(result)
    ev = result.evidence
    assert [r.prompt_version for r in ev.eligible_runs] == ["v3"]  # v2 is not approved; 11/25 run excluded
    assert ev.metric("pass_rate").value == pytest.approx(0.96)    # from the full v3 run, not the partial 0.909
    assert risks["H02"].status == MITIGATED
    assert risks["H09"].status == GAP                             # only one full v3 run to compare
    assert result.decision == "CONDITIONAL GO"


# ----------------------------------------------------------------- evidence rules

def test_judge_metrics_from_a_judge_stopped_run_do_not_count(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD)
    write_run(tmp_path, "2026-10-07 02:00 UTC", {**GOOD, "faithfulness": 0.2}, reasons=[JUDGE_STOPPED])
    ev = run(cfg, tmp_path).evidence
    assert ev.metric("faithfulness").value == 0.9            # older full measurement wins
    assert ev.metric("faithfulness").run.run_at.day == 6
    assert ev.metric("fact_accuracy").run.run_at.day == 7    # deterministic metric from the newer run


def test_partial_coverage_run_is_excluded_entirely(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD)
    write_run(tmp_path, "2026-10-07 02:00 UTC", {**GOOD, "safety_pass_rate": 0.5}, reasons=[PARTIAL])
    result = run(cfg, tmp_path)
    assert by_id(result)["R06"].status == MITIGATED
    assert len(result.evidence.excluded_runs) == 1


def test_all_mitigated_is_go(cfg, tmp_path):
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
    assert result.decision == "NO-GO"


def test_stale_evidence_becomes_a_gap(cfg, tmp_path):
    write_run(tmp_path, "2026-09-01 02:00 UTC", GOOD)
    result = run(cfg, tmp_path)
    assert by_id(result)["R02"].status == GAP
    assert result.decision == "NO-GO"


def test_evidence_from_another_model_does_not_count(cfg, tmp_path):
    write_run(tmp_path, "2026-10-06 02:00 UTC", GOOD, model="llama-3.1-8b-instant")
    write_run(tmp_path, "2026-10-07 02:00 UTC", GOOD, model="llama-3.1-8b-instant")
    risks = by_id(run(cfg, tmp_path))
    assert risks["R09"].status == OPEN
    assert risks["R01"].status == GAP


def test_missing_supporting_evidence_only_adds_a_note(cfg, tmp_path):
    no_recall = {k: v for k, v in GOOD.items() if k != "contextual_recall"}
    write_run(tmp_path, "2026-10-06 02:00 UTC", no_recall)
    write_run(tmp_path, "2026-10-07 02:00 UTC", no_recall)
    r07 = by_id(run(cfg, tmp_path))["R07"]
    assert r07.status == MITIGATED
    assert any("contextual_recall" in n for n in r07.notes)


# ----------------------------------------------------------------- config integrity

def test_rating_bands(cfg):
    policy = cfg["policy"]
    assert [rating_for(s, policy) for s in (1, 5, 10, 15, 25)] == ["low", "medium", "high", "critical", "critical"]


def test_decide_with_no_risks():
    assert decide([])[0] == "GO"


@pytest.mark.parametrize("sid", SYSTEMS)
def test_registers_are_consistent(sid):
    cfg = cfg_for(sid)
    defined = {sub for subs in cfg["nist"].values() for sub in subs}
    risks = cfg["register"]["risks"]
    assert cfg["register"]["system"] == sid == cfg["system"]["id"]
    assert len({r["id"] for r in risks}) == len(risks)
    for r in risks:
        assert set(r["nist"]) <= defined, r["id"]
        assert r.get("evidence") or r.get("acceptance"), f"{r['id']} needs evidence or a documented acceptance"


# ----------------------------------------------------------------- end to end

def test_cli_writes_all_outputs(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "README.md").write_text("intro\n<!-- READINESS:START -->\nold\n<!-- READINESS:END -->\n")
    code = main(["--evidence-root", str(FIXTURES), "--root", str(tmp_path), "--as-of", "2026-10-07 08:00"])
    assert code == 0
    for sid in SYSTEMS:
        for name in ("MODEL_CARD.md", "RISK_REGISTER.md", "NIST_AI_RMF.md", "RELEASE_DECISION.md"):
            assert (tmp_path / "reports" / sid / name).read_text().startswith("<!-- Generated")
        html = (tmp_path / "docs" / f"{sid}.html").read_text()
        assert "<script" not in html and 'href="index.html"' in html
    overview = (tmp_path / "docs" / "index.html").read_text()
    assert "NO-GO" in overview and "CONDITIONAL GO" in overview
    readme = (tmp_path / "README.md").read_text()
    assert "old" not in readme and "Tayal Capital" in readme and "Arya Bank" in readme
    assert main(["--evidence-root", str(FIXTURES), "--root", str(tmp_path), "--as-of", "2026-10-07 08:00", "--strict"]) == 1
