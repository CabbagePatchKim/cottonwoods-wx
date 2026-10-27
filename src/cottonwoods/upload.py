"""Upload the published feed to the website's server (a VPS) over SFTP, using Windows' built-in OpenSSH.

Only scripts/publish_feed.py calls this, and only after the approval gate passed, so preview/development output is
never uploaded. Settings: the optional config section `feed_upload` (host, user, port, key_file, remote_dir); with
no such section nothing is uploaded.

Safety:
  - A dedicated SSH key (no password, so the scheduled task can use it) that the server accepts ONLY for this upload
    account, ideally SFTP-only and locked to the feed folder.
  - BatchMode + StrictHostKeyChecking: it never prompts and refuses a server whose identity changed. The server's
    key must already be in known_hosts (the owner connects once by hand and checks the fingerprint with the friend).
  - Each file is uploaded under a temporary name, then renamed over the old one, so the site never serves half a file.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def sftp_batch(local_dir: Path, remote_dir: str, names: list[str]) -> str:
    """The sftp commands that upload <local_dir>/<name> to <remote_dir>/<local_dir's name>/<name> for each name.
    A leading '-' lets a command fail without stopping (mkdir of a folder that already exists)."""
    sub = f"{remote_dir.rstrip('/')}/{Path(local_dir).name}"
    lines = [f'-mkdir "{sub}"']
    for n in names:
        lines.append(f'put "{(Path(local_dir) / n).as_posix()}" "{sub}/.{n}.tmp"')
        lines.append(f'rename "{sub}/.{n}.tmp" "{sub}/{n}"')
    return "\n".join(lines) + "\n"


def sftp_command(ucfg: dict, batch_file: Path) -> list[str]:
    return [ucfg.get("sftp_exe", "sftp"), "-b", str(batch_file), "-i", str(Path(ucfg["key_file"]).expanduser()),
            "-P", str(ucfg.get("port", 22)), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=30", f"{ucfg['user']}@{ucfg['host']}"]


def upload_feed(ucfg: dict, local_dir: Path, run=subprocess.run) -> str:
    """Upload every .json in local_dir. Returns a one-line result; raises RuntimeError with sftp's message on failure."""
    names = sorted(p.name for p in Path(local_dir).glob("*.json"))
    with tempfile.TemporaryDirectory() as d:
        batch = Path(d) / "upload.sftp"
        batch.write_text(sftp_batch(Path(local_dir), ucfg["remote_dir"], names), encoding="utf-8")
        r = run(sftp_command(ucfg, batch), capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"upload to {ucfg['host']} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return f"uploaded {len(names)} files to {ucfg['host']}:{ucfg['remote_dir']}"
