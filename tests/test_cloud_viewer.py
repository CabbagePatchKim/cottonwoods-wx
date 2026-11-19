"""The owner's private phone viewer (cloud/viewer_worker.js) reads what the pass saves; keep the two in step.
The Worker itself is exercised end to end by `node tests/viewer_worker_check.mjs` (fake bucket, real Access tokens);
CI has no Node, so these checks are on the source."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKER = (ROOT / "cloud/viewer_worker.js").read_text(encoding="utf-8")
PASS = (ROOT / ".github/workflows/pass.yml").read_text(encoding="utf-8")


def test_viewer_reads_the_log_lines_the_pass_writes():
    assert 'const PASS_MARK = "===== "' in WORKER and 'echo "===== $(date -u' in PASS
    assert "code $(git describe" in PASS                       # parsePasses reads "<time> UTC code <version>"
    assert 'const STEPS_MARK = "----- steps: "' in WORKER and 'echo "----- steps: models' in PASS
    assert 'const LOG_PREFIX = "data/logs/cloud/models_"' in WORKER and "data/logs/cloud/models_$(date -u +%Y-%m).log" in PASS


def test_viewer_reads_files_the_bucket_actually_gets():
    cfg = yaml.safe_load((ROOT / "config/stations.yaml").read_text(encoding="utf-8"))
    assert "reports" in cfg["cloud"]["dirs"] and "logs/cloud" in cfg["cloud"]["dirs"]
    for name in ("storms_latest.html", "storms_latest.txt", "verified_latest.txt", "models_latest.txt"):
        assert f'"data/reports/{name}"' in WORKER


def test_viewer_is_private_and_fails_closed():
    # Collins totals are in the verified report: RECORD, NEVER PUBLISH. Every request needs a valid Access token.
    fetch = WORKER[WORKER.index("async fetch(request, env)"):]
    assert fetch.index("checkAccess(request, env)") < fetch.index("homePage")
    assert 'if (!team || !aud || !allowed.length) return { ok: false' in WORKER    # unconfigured -> 403
    for check in ("bad signature", "wrong audience", "wrong issuer", "expired", "not allowed"):
        assert check in WORKER
    assert '"Cache-Control": "private, no-store"' in WORKER and "noindex" in WORKER
    assert "script-src" not in WORKER and "<script" not in WORKER              # no scripts on any page
