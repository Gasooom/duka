"""Gate: the versioned agent evaluation suite (evals/cases_v1.json) must not regress. Runs offline with the rules
engine and with the adversarial 'always lies' model; the real-model run needs LLM credentials (evals/run.py)."""
import json
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from evals.harness import regressions, run_suite

EVALS = Path(__file__).resolve().parents[1] / "evals"
SUITE = EVALS / "cases_v1.json"


@pytest.mark.parametrize("provider", ["rules", "adversarial"])
def test_eval_suite_has_no_regressions_and_no_critical_failures(provider):
    report = run_suite(SessionLocal, provider, SUITE)
    baseline = json.loads((EVALS / "baselines" / f"{provider}.json").read_text())
    assert baseline["version"] == report["version"], "suite version changed: regenerate the baseline deliberately"
    assert report["critical_failures"] == [], report["critical_failures"]
    assert regressions(report, baseline) == []


def test_adversarial_model_is_caught_on_every_reply_it_writes():
    report = run_suite(SessionLocal, "adversarial", SUITE)
    for case in report["cases"]:
        for turn in case["transcript"]:
            if turn["run_status"] == "success":  # only server-rendered checkout summaries may pass unchanged
                assert all(r.startswith("🧾 Order summary") for r in turn["replies"]), (case["id"], turn)


def test_gate_catches_a_safety_regression(monkeypatch):
    """If someone disabled the grounding check, the suite must fail loudly."""
    monkeypatch.setattr("app.agents.engine.verify", lambda text, ledger: [])
    report = run_suite(SessionLocal, "adversarial", SUITE)
    baseline = json.loads((EVALS / "baselines" / "adversarial.json").read_text())
    assert report["critical_failures"] and regressions(report, baseline)
