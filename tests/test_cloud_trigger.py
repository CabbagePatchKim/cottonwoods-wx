"""The Cloudflare Worker that starts the cloud pass (cloud/trigger_worker.js) stays in step with pass.yml."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = (ROOT / "cloud/trigger_worker.js").read_text()
PASS = (ROOT / ".github/workflows/pass.yml").read_text()


def _hours(field: str) -> set[int]:
    """A cron hour field: '2-23/3' or '2,5,8'."""
    if "/" in field:
        span, step = field.split("/")
        lo, hi = map(int, span.split("-"))
        return set(range(lo, hi + 1, int(step)))
    return {int(h) for h in field.split(",")}


def test_worker_fires_five_minutes_after_every_github_slot():
    gh_min, gh_hours = re.search(r'cron: "(\d+) (\S+) \* \* \*"', PASS).groups()
    cf_min, cf_hours = re.search(r'const CRON = "(\d+) (\S+) \* \* \*"', WORKER).groups()
    assert _hours(cf_hours) == _hours(gh_hours)
    assert int(cf_min) == int(gh_min) + 5


def test_worker_dispatches_this_workflow():
    assert 'const WORKFLOW = "pass.yml"' in WORKER
    assert "workflow_dispatch" in PASS            # the dispatch API needs this trigger


def test_a_late_scheduled_run_after_another_pass_is_skipped_but_manual_runs_never_are():
    gate = PASS[PASS.index("  gate:"):PASS.index("  pass:")]
    assert 'github.event_name }}" = schedule' in gate and "-lt 2400" in gate      # scheduled runs only, 40 min
    assert "needs: gate" in PASS and "if: needs.gate.outputs.skip != 'true'" in PASS
    assert "actions: read" in PASS                                                # to list the workflow's runs
