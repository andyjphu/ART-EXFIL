"""Read committed spreadsheets from every branch of a public Git repository.

Only Git trees and regular ``.xlsx`` blobs are inspected. The output contains
copies of those blobs and a manifest; it does not inspect the workbook contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


MAX_BRANCHES = 64
MAX_FILES = 256
MAX_BLOB_BYTES = 25_000_000
MAX_TOTAL_BYTES = 64_000_000

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(_GIT_ENV)
    env["GIT_ASKPASS"] = shutil.which("false") or os.devnull
    return env


def _git(*args: str, cwd: Path | None = None) -> bytes:
    command = ["git", "-c", "credential.helper=", "--no-pager"]
    if cwd is not None:
        command += ["-C", str(cwd)]
    command += list(args)
    result = subprocess.run(command, capture_output=True, env=_git_env(), check=False)
    if result.returncode:
        raise RuntimeError(
            f"git {args[0]} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')[-1500:]}"
        )
    if args[0] == "clone" and b"filtering not recognized by server" in result.stderr.lower():
        raise RuntimeError("Git server did not support blob filtering; refusing a full-blob clone")
    return result.stdout


def _validate_url(url: str, allow_file_url: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https"):
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Git URL must have a host and no embedded credentials")
    elif parsed.scheme == "file" and allow_file_url:
        if parsed.netloc not in ("", "localhost") or not parsed.path:
            raise ValueError("Local test URL must be a file:// URL")
    else:
        raise ValueError("Use an unauthenticated http(s) Git URL")
    if parsed.query or parsed.fragment:
        raise ValueError("Git URL cannot contain a query or fragment")


def _prepare_output(output_dir: Path) -> Path:
    if output_dir.is_symlink():
        raise ValueError("Output directory cannot be a symlink")
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory must be empty: {output_dir}")
    output_dir.chmod(0o700)
    blobs = output_dir / "blobs"
    blobs.mkdir(mode=0o700)
    return blobs


def _branches(repo: Path) -> list[tuple[str, str]]:
    raw = _git("for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads", cwd=repo)
    branches = []
    for line in raw.splitlines():
        if not line:
            continue
        ref, commit = line.split(b"\0", 1)
        branches.append((ref.decode("utf-8", "surrogateescape"), commit.decode("ascii")))
    return branches


def _spreadsheet_blobs(repo: Path, commit: str) -> list[tuple[str, str]]:
    raw = _git("ls-tree", "-r", "-z", "--full-tree", commit, cwd=repo)
    found = []
    for row in raw.split(b"\0"):
        if not row:
            continue
        header, path = row.split(b"\t", 1)
        mode, kind, blob_sha = header.split(b" ")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            continue
        if path.lower().endswith(b".xlsx"):
            found.append((path.decode("utf-8", "surrogateescape"), blob_sha.decode("ascii")))
    return found


def _save_blob(repo: Path, blob_sha: str, blobs: Path, max_bytes: int) -> tuple[str, int, bool]:
    if max_bytes < 0:
        raise RuntimeError("Attacker spreadsheet scan exceeded its total byte budget")
    command = ["git", "-c", "credential.helper=", "--no-pager", "-C", str(repo), "cat-file", "blob", blob_sha]
    with tempfile.NamedTemporaryFile(prefix=".part-", suffix=".xlsx", dir=blobs, delete=False) as temporary:
        part = Path(temporary.name)
        digest = hashlib.sha256()
        size = 0
        first_bytes = b""
        try:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env()) as process:
                assert process.stdout is not None
                while chunk := process.stdout.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        process.kill()
                        process.wait()
                        raise RuntimeError("Attacker spreadsheet blob exceeds its byte budget")
                    if not first_bytes:
                        first_bytes = chunk[:80]
                    temporary.write(chunk)
                    digest.update(chunk)
                assert process.stderr is not None
                error = process.stderr.read()
                if process.wait():
                    raise RuntimeError(f"git cat-file failed: {error.decode('utf-8', 'replace')[-1500:]}")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    sha256 = digest.hexdigest()
    destination = blobs / f"{sha256}.xlsx"
    if destination.is_symlink():
        part.unlink(missing_ok=True)
        raise ValueError("Blob destination cannot be a symlink")
    if destination.exists():
        part.unlink()
    else:
        part.replace(destination)
        destination.chmod(0o600)
    is_lfs_pointer = first_bytes.startswith(b"version https://git-lfs.github.com/spec/v1\n")
    return sha256, size, is_lfs_pointer


def scan_public_repo(url: str, output_dir: str | Path, *, allow_file_url: bool = False) -> dict:
    """Copy all committed branch ``.xlsx`` blobs and return the output manifest.

    ``allow_file_url`` is for isolated local-repository tests. The Git cache is
    removed after the scan; neither markers nor non-spreadsheet blobs are opened.
    """
    _validate_url(url, allow_file_url)
    output = Path(output_dir)
    blobs = _prepare_output(output)
    entries: list[dict] = []
    copied: dict[str, tuple[str, int, bool]] = {}
    with tempfile.TemporaryDirectory(prefix=".git-scan-", dir=output) as scratch:
        repo = Path(scratch) / "repo.git"
        _git("clone", "--bare", "--no-tags", "--filter=blob:none", "--no-single-branch", url, str(repo))
        branches = _branches(repo)
        if len(branches) > MAX_BRANCHES:
            raise RuntimeError("Attacker spreadsheet scan exceeded its branch limit")
        total_saved = 0
        for ref, commit in branches:
            for path, blob_sha in _spreadsheet_blobs(repo, commit):
                if len(entries) >= MAX_FILES:
                    raise RuntimeError("Attacker spreadsheet scan exceeded its file limit")
                if blob_sha not in copied:
                    copied[blob_sha] = _save_blob(
                        repo, blob_sha, blobs, min(MAX_BLOB_BYTES, MAX_TOTAL_BYTES - total_saved)
                    )
                    total_saved += copied[blob_sha][1]
                sha256, size, lfs_pointer = copied[blob_sha]
                entries.append(
                    {
                        "ref": ref,
                        "commit": commit,
                        "path": path,
                        "git_blob_sha": blob_sha,
                        "sha256": sha256,
                        "size": size,
                        "stored_path": f"blobs/{sha256}.xlsx",
                        "lfs_pointer": lfs_pointer,
                    }
                )
    manifest = {"schema_version": 1, "branches": len(branches), "files": entries}
    manifest_path = output / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    manifest_path.chmod(0o600)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Read committed .xlsx blobs from public Git branches")
    parser.add_argument("--repo-url", required=True, help="Unauthenticated http(s) Git URL")
    parser.add_argument("--output-dir", required=True, help="New empty attacker-only output directory")
    args = parser.parse_args()
    manifest = scan_public_repo(args.repo_url, args.output_dir)
    print(json.dumps({"branches": manifest["branches"], "files": len(manifest["files"]), "manifest": str(Path(args.output_dir) / "manifest.json")}))


if __name__ == "__main__":
    main()
