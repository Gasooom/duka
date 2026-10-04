"""Run the agent evaluation suite.

    python -m evals.run --provider rules
    python -m evals.run --provider adversarial
    python -m evals.run --provider openai_compat --out evals/reports/gpt-4o-mini.json     # needs LLM_* settings
    python -m evals.run --provider rules --update-baseline                               # after an intended change

Uses EVAL_DATABASE_URL (default: the commerce_eval database next to DATABASE_URL); the schema is rebuilt from
migrations and wiped between cases, so it must never point at real data (the harness refuses names without
'test' or 'eval'). Exit code 1 when a critical case fails or a case that passes in the baseline regresses.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run")
    ap.add_argument("--provider", choices=["rules", "adversarial", "openai_compat"], default="rules")
    ap.add_argument("--suite", default=str(ROOT / "cases_v1.json"))
    ap.add_argument("--out", help="write the full report (with transcripts) here")
    ap.add_argument("--baseline", help="compare with this report (default: evals/baselines/<provider>.json)")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--include-llm-cases", action="store_true", help="also run cases marked requires_llm")
    args = ap.parse_args(argv)

    base_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://commerce:commerce@localhost:5432/commerce")
    os.environ["DATABASE_URL"] = os.environ.get("EVAL_DATABASE_URL", base_url.rsplit("/", 1)[0] + "/commerce_eval")
    os.environ["BACKGROUND_WORKERS"] = "0"
    os.environ.setdefault("APP_ENV", "test")
    if args.provider != "openai_compat":
        os.environ["LLM_PROVIDER"] = "rules"

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    url = make_url(os.environ["DATABASE_URL"])
    with create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT").connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": url.database}):
            c.execute(text(f'CREATE DATABASE "{url.database}"'))

    from alembic.config import Config

    from alembic import command
    cfg = Config(str(ROOT.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT.parent / "alembic"))
    command.upgrade(cfg, "head")

    from app.db.session import SessionLocal
    from evals.harness import regressions, run_suite
    report = run_suite(SessionLocal, args.provider, Path(args.suite),
                       include_llm_cases=True if args.include_llm_cases else None)

    t = report["totals"]
    print(f"{report['suite']} v{report['version']} · provider={report['provider']} model={report['model']} "
          f"· prompt={report['prompt_fingerprint']} · {report['duration_s']}s")
    print(f"passed {t['passed']} · failed {t['failed']} · skipped {t['skipped']} · pass rate {t['pass_rate']}")
    for cat, c in sorted(report["by_category"].items()):
        print(f"  {cat:<18} pass {c['pass']:>2}  fail {c['fail']:>2}  skip {c['skip']:>2}")
    m = report["metrics"]
    print(f"  turns {m['turns']} · model turns {m['model_turns']} · ungrounded {m['ungrounded_turns']} "
          f"(rate {m['ungrounded_rate']}) · agent errors {m['agent_errors']} · p50 latency {m['latency_ms_p50']} ms")
    for c in report["cases"]:
        if c["status"] == "fail":
            print(f"  FAIL {c['id']}: " + "; ".join(c["failures"]))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    baseline_path = Path(args.baseline or ROOT / "baselines" / f"{args.provider}.json")
    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in report.items() if k != "cases"} | {
            "cases": [{"id": c["id"], "status": c["status"]} for c in report["cases"]]}
        baseline_path.write_text(json.dumps(slim, indent=2), encoding="utf-8")
        print(f"baseline written: {baseline_path}")
    regressed = regressions(report, json.loads(baseline_path.read_text())) if baseline_path.exists() else []
    if regressed:
        print(f"REGRESSIONS vs {baseline_path.name}: {regressed}")
    if report["critical_failures"]:
        print(f"CRITICAL FAILURES: {report['critical_failures']}")
    return 1 if regressed or report["critical_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
