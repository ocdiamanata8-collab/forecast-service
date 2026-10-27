"""Repository guard for workflow scheduling and alert wiring."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _schedule(workflow: dict) -> list[str]:
    # PyYAML's YAML 1.1 parser reads unquoted `on` as boolean True.
    on = workflow.get("on", workflow.get(True, {}))
    return [entry["cron"] for entry in on.get("schedule", [])]


def test_forecast_workflow_has_hourly_forecast_and_backfill_daily_retention():
    wf = _workflow("forecast-jobs.yml")
    schedules = _schedule(wf)
    assert schedules == ["17 * * * *", "37 * * * *", "23 3 * * *"]
    assert all(cron.split()[0] != "0" for cron in schedules), "jadwal harus off-minute"
    jobs = wf["jobs"]
    assert jobs["forecast"]["if"] == "github.event.schedule == '17 * * * *'"
    assert jobs["backfill"]["if"] == "github.event.schedule == '37 * * * *'"
    assert "github.event.schedule == '23 3 * * *'" in jobs["retention"]["if"]
    assert "vars.FORECAST_RETENTION_DAYS != '0'" in jobs["retention"]["if"]
    assert "vars.FORECAST_RETENTION_DAYS != ''" in jobs["retention"]["if"]


def test_forecast_action_is_http_only_and_uses_explicit_horizon():
    wf = _workflow("forecast-jobs.yml")
    forecast = wf["jobs"]["forecast"]
    env = forecast["steps"][1]["env"]
    assert env["FORECAST_HORIZON"] == "24"
    text = str(forecast)
    assert "bash scripts/run_forecast.sh" in text
    assert "setup-python" not in text
    assert "pip install" not in text
    assert "model.pkl" not in text
    assert "secrets.FORECAST_API_KEY" in text


def test_alert_workflow_is_independent_and_uses_secrets():
    wf = _workflow("forecast-monitor.yml")
    schedules = _schedule(wf)
    assert schedules == ["5,20,35,50 * * * *"]
    assert all(minute != "0" for minute in schedules[0].split()[0].split(","))
    assert wf["permissions"]["contents"] == "read"
    assert wf["concurrency"]["group"] == "forecast-monitor"
    job = wf["jobs"]["monitor"]
    text = str(job)
    assert "python -m app.monitoring" in text
    assert "secrets.DATABASE_URL" in text
    assert "secrets.FORECAST_ALERT_WEBHOOK_URL" in text
    assert "app/monitoring.py" not in text  # workflow must not mask failure/replace CLI


def test_no_web_process_interval_loops_remain():
    root = ROOT / "app"
    source = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    assert "async def scheduler_loop" not in source
    assert "async def backfill_loop" not in source
    assert "async def retention_loop" not in source
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "asyncio.create_task" not in main
    assert "scheduler_task = None" in main
    assert "backfill_task = None" in main
    assert "retention_task = None" in main
