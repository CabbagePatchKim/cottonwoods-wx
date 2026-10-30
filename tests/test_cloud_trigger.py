"""The Cloudflare Worker (cloud/trigger_worker.js) is the only thing that starts the cloud pass (pass.yml)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = (ROOT / "cloud/trigger_worker.js").read_text()
PASS = (ROOT / ".github/workflows/pass.yml").read_text()


def test_worker_fires_every_three_hours_at_ten_to():
    assert 'const CRON = "50 2,5,8,11,14,17,20,23 * * *"' in WORKER


def test_worker_dispatches_this_workflow_and_github_schedule_is_gone():
    assert 'const WORKFLOW = "pass.yml"' in WORKER
    assert "workflow_dispatch" in PASS            # the dispatch API needs this trigger
    assert "schedule:" not in PASS and "cron:" not in PASS     # owner 2026-10-29: the Worker is the only trigger
