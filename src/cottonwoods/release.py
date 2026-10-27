"""The approval gate for the website feed: only code the owner approved may publish.

The owner approves a version by having it tagged as a release (an ANNOTATED git tag like v1.0, made only on the
owner's explicit "approve"). The publish step calls approved_version() first and stops unless ALL of these hold:
  - the code actually running (the imported cottonwoods package) lives in the checkout being checked, so a
    development install can't sneak in;
  - that checkout's current commit is exactly an annotated tag named vMAJOR.MINOR[.PATCH];
  - the checkout has no uncommitted or untracked changes (ignored files like data/ don't count).
Development (branch main, unreleased commits, or edits in progress) therefore can never publish, even by hand.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import cottonwoods

TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")


class NotApproved(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def approved_version(root: Path, package_file: str | None = None) -> str:
    """The approved release tag (e.g. 'v1.0') of the checkout at `root`, or NotApproved explaining why not."""
    root = Path(root).resolve()
    pkg = Path(package_file or cottonwoods.__file__).resolve()
    if root / "src" not in pkg.parents:
        raise NotApproved(f"the running code ({pkg.parent}) is not the checkout being released ({root})")
    tags = [t for t in _git(root, "tag", "--points-at", "HEAD").splitlines() if TAG_RE.match(t)]
    tags = [t for t in tags if _git(root, "cat-file", "-t", t) == "tag"]   # annotated tags only
    if not tags:
        raise NotApproved("this code is not an approved release (no annotated vX.Y tag on the current commit)")
    if _git(root, "status", "--porcelain"):
        raise NotApproved("this checkout has uncommitted changes, so it isn't exactly the approved release")
    return sorted(tags, key=lambda t: [int(x) for x in t[1:].split(".")])[-1]


def dev_version(root: Path) -> str:
    """A label for previews: 'dev-<short commit>' plus '-modified' if there are uncommitted changes."""
    sha = _git(root, "rev-parse", "--short", "HEAD") or "unknown"
    return f"dev-{sha}" + ("-modified" if _git(root, "status", "--porcelain") else "")
