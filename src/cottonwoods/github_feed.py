"""Publish the feed to the owner's GitHub Pages repository, so any website can read it from a fixed public URL
(e.g. https://<user>.github.io/cwx-feed/v1/storms.json). The website's owner gives us nothing; their site fetches.

Only scripts/publish_feed.py calls this, and only after the approval gate passed, so preview/development output is
never pushed. Settings: the optional config section `feed_github` (repo, key_file, known_hosts, branch); with no such
section nothing is pushed.

How: a clone of the feed repo lives in data/feed_repo (it holds ONLY generated files, so it's simply reset to the
remote before each update). The new v1/*.json are copied in, with the release's user manual as the repo's README.md
(owner, 2026-10-29: the website's owner reads the repo, so each published version carries its own manual), then
committed and pushed. Unchanged files = no commit.
Safety: a deploy key that can write to that one repository only; BatchMode (never prompts) and StrictHostKeyChecking
against a known_hosts file holding GitHub's published host key.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AUTHOR = ["-c", "user.name=cottonwoods-wx", "-c", "user.email=feed@cottonwoods-wx.invalid"]


def ssh_command(gcfg: dict) -> str:
    key = Path(gcfg["key_file"]).expanduser().as_posix()
    kh = Path(gcfg["known_hosts"]).expanduser().as_posix()
    ssh = gcfg.get("ssh_exe") or ("C:/Windows/System32/OpenSSH/ssh.exe" if os.name == "nt" else "ssh")
    return (f'"{ssh}" -i "{key}" -o IdentitiesOnly=yes '
            f'-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="{kh}" -o ConnectTimeout=30')


def publish(gcfg: dict, local_dir: Path, clone_dir: Path, version: str, generated: str, run=subprocess.run,
            readme: Path | None = None) -> str:
    """Push <local_dir>/*.json to <repo>/<local_dir's name>/ on gcfg['branch'], and `readme` (the user manual) as the
    repo's README.md. Returns a one-line result; raises RuntimeError with git's message on failure."""
    env = {**os.environ, "GIT_SSH_COMMAND": ssh_command(gcfg)}
    branch = gcfg.get("branch", "main")

    def git(*args, cwd=clone_dir):
        r = run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
        return r.stdout

    clone_dir = Path(clone_dir)
    if not (clone_dir / ".git").exists():
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--branch", branch, gcfg["repo"], str(clone_dir), cwd=clone_dir.parent)
    git("fetch", "-q", "origin", branch)
    git("reset", "-q", "--hard", f"origin/{branch}")
    sub = clone_dir / Path(local_dir).name
    sub.mkdir(exist_ok=True)
    names = sorted(p.name for p in Path(local_dir).glob("*.json"))
    for n in names:
        shutil.copyfile(Path(local_dir) / n, sub / n)
    if readme is not None:
        shutil.copyfile(readme, clone_dir / "README.md")
    (clone_dir / ".nojekyll").touch()   # serve files as-is (GitHub Pages would otherwise run Jekyll)
    git("add", "-A")
    if not git("status", "--porcelain").strip():
        return "feed unchanged; nothing pushed"
    git(*AUTHOR, "commit", "-q", "-m", f"feed {version}, {generated}")
    git("push", "-q", "origin", f"HEAD:{branch}")
    return f"pushed {len(names)} files to {gcfg['repo']} ({branch})"
