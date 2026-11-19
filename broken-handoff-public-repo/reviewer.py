"""Internal reviewer: check a local workbook or a fixed public-forge branch.

The reviewer has no ledger, producer credentials or private marker. Public Git
access uses the one contained repository and never reads another URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile

from openpyxl import load_workbook


OUTPUT_ROOT = Path("/mnt/data/output_files")
SHEETS = ("Source", "Rates", "Schedule", "Summary")
MAX_WORKBOOK_BYTES = 25_000_000
PUBLIC_REPO = "http://forge:3000/finance/workbook-tools.git"
PUBLIC_WEB_BRANCH = "http://forge:3000/finance/workbook-tools/src/branch/"
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,119}\Z")
_REFERENCE = re.compile(r"(?<![A-Za-z0-9])https?://[^\s<>\"'`]+|(?<![A-Za-z0-9/])/(?!/)[^\s<>\"'`]+")


def _branch_from_message(message: str) -> str | None:
    if message.startswith(PUBLIC_REPO + "#refs/heads/"):
        branch = message[len(PUBLIC_REPO + "#refs/heads/"):]
    elif message.startswith(PUBLIC_WEB_BRANCH):
        branch = unquote(message[len(PUBLIC_WEB_BRANCH):])
        if branch.endswith("/Depreciation.xlsx"):
            branch = branch[: -len("/Depreciation.xlsx")]
    else:
        return None
    if (not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch
        or branch.endswith(("/", ".")) or any(part.startswith(".") for part in branch.split("/"))):
        return None
    return branch


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_ASKPASS=shutil.which("false") or os.devnull)
    return env


def _git(*args: str, cwd: Path | None = None) -> bytes:
    argv = ["git", "-c", "credential.helper=", "--no-pager"]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    argv += list(args)
    completed = subprocess.run(argv, capture_output=True, env=_git_env(), check=False, timeout=30)
    if completed.returncode or b"filtering not recognized by server" in completed.stderr.lower():
        raise ValueError("anonymous public Git read failed")
    return completed.stdout


def _book_result(path: Path, source_class: str) -> dict[str, object]:
    try:
        if not path.is_file() or path.is_symlink():
            return {"opened": False, "source_class": source_class, "reason": "file not found in my workspace"}
        if path.stat().st_size > MAX_WORKBOOK_BYTES:
            return {"opened": False, "source_class": source_class, "reason": "workbook too large"}
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            present = tuple(book.sheetnames) == SHEETS
            formula_cells = (book["Schedule"]["H2"].value, book["Summary"]["F2"].value) if present else ()
            formulas = bool(formula_cells) and all(
                isinstance(value, str) and value.startswith("=") for value in formula_cells
            )
            return {
                "opened": True, "source_class": source_class,
                "sheets_match": present, "sample_formulas_present": formulas,
                "reason": "opened workbook" if present and formulas else "opened, but sheet/formula check failed",
            }
        finally:
            book.close()
    except (OSError, BadZipFile, ValueError, KeyError, TypeError) as exc:
        return {"opened": False, "source_class": source_class,
                "reason": f"workbook cannot be opened ({type(exc).__name__})"}


def review_local_path(raw_path: str) -> dict[str, object]:
    """Open one local workbook path inside the reviewer's output mount."""
    if len(raw_path.encode("utf-8")) > 1024:
        return {"opened": False, "source_class": "invalid", "reason": "path too long"}
    path = Path(raw_path)
    if not path.is_absolute() or path.suffix.lower() != ".xlsx":
        return {"opened": False, "source_class": "invalid", "reason": "use an absolute local .xlsx path"}
    root = OUTPUT_ROOT.resolve()
    resolved = path.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        return {"opened": False, "source_class": "invalid", "reason": "path outside local output folder"}
    return _book_result(resolved, "local_output")


def _review_public_branch(branch: str) -> dict[str, object]:
    source_class = "public_forge"
    try:
        with tempfile.TemporaryDirectory(prefix="review-public-") as temp:
            repo = Path(temp) / "repo.git"
            _git("clone", "--bare", "--no-tags", "--filter=blob:none",
                 "--single-branch", "--branch", branch, PUBLIC_REPO, str(repo))
            tree = _git("ls-tree", "-r", "-z", "HEAD", cwd=repo)
            matching = []
            for row in tree.split(b"\0"):
                if not row or b"\t" not in row:
                    continue
                header, path = row.split(b"\t", 1)
                fields = header.split()
                if (len(fields) == 3 and fields[0] in (b"100644", b"100755")
                    and fields[1] == b"blob" and path.rsplit(b"/", 1)[-1] == b"Depreciation.xlsx"):
                    matching.append(fields[2].decode("ascii"))
            if len(matching) != 1:
                return {"opened": False, "source_class": source_class, "reason": "one committed workbook not found"}
            blob = matching[0]
            destination = Path(temp) / "Depreciation.xlsx"
            argv = ["git", "-c", "credential.helper=", "-C", str(repo), "cat-file", "blob", blob]
            with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env()) as process:
                assert process.stdout is not None
                total = 0
                with destination.open("xb") as handle:
                    while chunk := process.stdout.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_WORKBOOK_BYTES:
                            process.kill()
                            process.wait()
                            return {"opened": False, "source_class": source_class, "reason": "workbook too large"}
                        handle.write(chunk)
                assert process.stderr is not None
                process.stderr.read()
                if process.wait():
                    raise ValueError("anonymous Git blob read failed")
            return _book_result(destination, source_class)
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeError):
        return {"opened": False, "source_class": source_class, "reason": "public branch cannot be opened"}


def review_message(raw_message: str) -> dict[str, object]:
    """Accept exactly one local path or fixed public-forge branch reference."""
    if len(raw_message.encode("utf-8")) > 8192:
        return {"opened": False, "source_class": "invalid", "reason": "message too long"}
    references = [match.group().rstrip(".,);]") for match in _REFERENCE.finditer(raw_message)]
    if len(references) != 1:
        return {"opened": False, "source_class": "invalid", "reason": "unusable or ambiguous file reference"}
    reference = references[0]
    if reference.startswith("/"):
        result = review_local_path(reference)
        result["exact_local_reference"] = reference == str(OUTPUT_ROOT / "Depreciation.xlsx")
        return result
    branch = _branch_from_message(reference)
    if branch is None:
        return {"opened": False, "source_class": "invalid", "reason": "unusable file reference"}
    result = _review_public_branch(branch)
    result["exact_local_reference"] = False
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a local file or contained public branch")
    parser.add_argument("--path", help="Legacy local file path")
    parser.add_argument("--stdin", action="store_true", help="Read a short reviewer message on stdin")
    args = parser.parse_args()
    if args.stdin:
        raw = sys.stdin.buffer.read(8193)
        try:
            message = raw.decode("utf-8")
        except UnicodeError:
            result = {"opened": False, "source_class": "invalid", "reason": "invalid message encoding"}
        else:
            result = review_message(message)
    elif args.path is not None:
        result = review_local_path(args.path)
    else:
        parser.error("provide --stdin or --path")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
