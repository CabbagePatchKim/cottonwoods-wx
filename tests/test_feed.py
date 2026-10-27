"""The website feed (feed.py): the v1 format contract, and the approval gate (release.py)."""
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from cottonwoods import feed, release, storms
from cottonwoods.archive import forecasts as fa
from cottonwoods.models import registry
from helpers import fake_ensemble, fake_puller, make_cfg

NOW = pd.Timestamp("2026-11-01 18:00")


def _world():
    """One fake deterministic run and one fake ensemble run, wet the whole time, and the storm they make."""
    cfg = make_cfg()
    init = datetime(2026, 11, 1, 12)
    orig = registry.REGISTRY["GEFS"]
    registry.REGISTRY["GEFS"] = registry.ModelSpec("GEFS", lambda i, c: list(range(6, 49, 6)), 7.0, members=3)
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fa.archive_run("HRRR", init, cfg, root=root, puller=lambda i, f, p, s=None: fake_puller()(i, list(range(1, 49)), p, s))
            fa.archive_run("GEFS", init, cfg, root=root, puller=fake_ensemble(3))
            fc = fa.load_forecasts(cfg, root=root)
    finally:
        registry.REGISTRY["GEFS"] = orig
    st = storms.update_forecast_storms(storms.load_storms(Path(tempfile.gettempdir()) / "no_such_dir"), fc, cfg, NOW)
    return cfg, fc, st


def _feed():
    cfg, fc, st = _world()
    return cfg, feed.build_feed(st, fc, cfg, NOW, "v9.9", ["WARNING test"])


def test_feed_v1_format_is_pinned():
    # These key sets ARE the v1 contract with the website. Adding a key is fine (add it here too); renaming or
    # removing one breaks the site: that needs a v2 feed instead.
    cfg, f = _feed()
    assert set(f) == {"meta", "storms", "daily"}
    for d in f.values():
        assert d["schema_version"] == 1 and d["generated"] == "2026-11-01T11:00:00-07:00"  # DST ended that morning
        json.dumps(d, allow_nan=False)  # plain JSON: no NaN, no numpy types
    m = f["meta"]
    assert {"code_version", "timezone", "units", "ensemble_wet_threshold_in", "snow_method", "points", "models",
            "warnings", "disclaimer", "credits"} <= set(m)
    assert m["code_version"] == "v9.9" and set(m["points"]) == set(cfg["points"])
    assert {"model": "HRRR", "cycle": "11/01 12z", "init": "2026-11-01T05:00:00-07:00"} in m["models"]
    s = f["storms"]
    assert {"snow_regime_adjusted", "storms", "recent_changes"} <= set(s) and len(s["storms"]) == 1
    st = s["storms"][0]
    assert {"id", "start", "end", "hours", "window_from", "notes", "models"} <= set(st)
    by = {r["model"]: r for r in st["models"]}
    assert {"model", "cycle", "init", "covers_full_storm", "run_ends", "snow_level_ft", "flow", "modes",
            "points"} <= set(by["HRRR"])
    assert set(by["HRRR"]["points"]) == set(cfg["points"])
    assert {"type", "water_in", "snow_in"} <= set(by["HRRR"]["points"]["alta"])
    assert by["HRRR"]["points"]["alta"]["type"] == "deterministic"
    assert {"type", "members_wet", "members", "mean_water_in"} <= set(by["GEFS"]["points"]["alta"])
    day = f["daily"]["points"]["alta"][0]
    assert {"model", "cycle", "init", "ensemble", "days"} <= set(day)
    assert {"start", "end", "amounts"} <= set(day["days"][0])


def test_runs_ending_before_the_storm_ends_have_no_amounts():
    cfg, fc, st = _world()
    st.loc[st["status"] == "forecast", "fc_end"] += pd.Timedelta(days=1)    # storm outlasts both 48 h runs
    s = feed.build_storms(st, fc, cfg)["storms"][0]
    for m in s["models"]:
        assert m["covers_full_storm"] is False and m["run_ends"] is not None
        assert all(v is None for v in m["points"].values())


def test_feed_files_are_written_whole_under_v1():
    _, f = _feed()
    with tempfile.TemporaryDirectory() as d:
        out = feed.write_feed(f, Path(d))
        assert out.name == "v1" and sorted(p.name for p in out.iterdir()) == ["daily.json", "meta.json", "storms.json"]
        assert json.loads((out / "meta.json").read_text(encoding="utf-8"))["code_version"] == "v9.9"


def _repo(d: Path) -> Path:
    git = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "src" / "cottonwoods").mkdir(parents=True)
    (d / "src" / "cottonwoods" / "__init__.py").write_text("")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    return d


def _refused(root, pkg):
    try:
        release.approved_version(root, pkg)
    except release.NotApproved:
        return True
    return False


def test_only_a_clean_annotated_release_tag_may_publish():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        pkg = str(root / "src" / "cottonwoods" / "__init__.py")
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
        assert _refused(root, pkg)                                   # development commit: no tag
        subprocess.run([*git, "tag", "v1.0"], check=True)
        assert _refused(root, pkg)                                   # lightweight tag isn't an approval
        subprocess.run([*git, "tag", "-a", "v1.1", "-m", "approved by owner"], check=True)
        assert release.approved_version(root, pkg) == "v1.1"
        (root / "src" / "cottonwoods" / "extra.py").write_text("x = 1")
        assert _refused(root, pkg)                                   # edits on top of the release
        (root / "src" / "cottonwoods" / "extra.py").unlink()
        assert _refused(root, str(Path(d).parent / "elsewhere" / "src" / "cottonwoods" / "__init__.py"))  # other code
        assert release.dev_version(root).startswith("dev-")


def test_github_publish_commits_changed_files_and_skips_unchanged():
    from cottonwoods import github_feed
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        remote, seed = d / "remote.git", d / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
        (seed / "README.md").write_text("feed")
        subprocess.run(["git", "-C", str(seed), *github_feed.AUTHOR, "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(seed), *github_feed.AUTHOR, "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"], check=True)
        out = d / "feed" / "v1"
        out.mkdir(parents=True)
        (out / "meta.json").write_text('{"a": 1}')
        gcfg = {"repo": str(remote), "branch": "main", "key_file": "k", "known_hosts": "kh"}
        assert "pushed 1 files" in github_feed.publish(gcfg, out, d / "clone", "v1.0", "now")
        assert "unchanged" in github_feed.publish(gcfg, out, d / "clone", "v1.0", "later")
        (out / "meta.json").write_text('{"a": 2}')
        assert "pushed" in github_feed.publish(gcfg, out, d / "clone", "v1.1", "later")
        shown = subprocess.run(["git", "-C", str(remote), "show", "main:v1/meta.json"], capture_output=True, text=True)
        log = subprocess.run(["git", "-C", str(remote), "log", "--format=%s", "main"], capture_output=True, text=True)
        assert shown.stdout == '{"a": 2}' and log.stdout.splitlines() == ["feed v1.1, later", "feed v1.0, now", "init"]
    assert "BatchMode=yes" in github_feed.ssh_command(gcfg) and "StrictHostKeyChecking=yes" in github_feed.ssh_command(gcfg)
