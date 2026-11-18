"""Local-only tests for the public Git spreadsheet reader."""

from __future__ import annotations

import contextlib
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import attacker_read
from attacker_read import scan_public_repo


class AttackerReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bare = self.root / "public.git"
        self.work = self.root / "work"
        self.output = self.root / "attacker-only"
        self.git("init", "--bare", "--initial-branch=main", str(self.bare))
        self.git("--git-dir", str(self.bare), "config", "uploadpack.allowFilter", "true")
        self.git("--git-dir", str(self.bare), "config", "uploadpack.allowAnySHA1InWant", "true")
        self.git("init", "--initial-branch=main", str(self.work))
        self.git("-C", str(self.work), "config", "user.name", "Local Fixture")
        self.git("-C", str(self.work), "config", "user.email", "fixture@example.invalid")
        self.git("-C", str(self.work), "remote", "add", "origin", self.bare.as_uri())

    @staticmethod
    def git(*args: str) -> None:
        subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit_and_push(self, message: str, branch: str) -> None:
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "-m", message)
        self.git("-C", str(self.work), "push", "origin", f"HEAD:refs/heads/{branch}")

    def test_reads_every_branch_and_only_committed_regular_spreadsheets(self) -> None:
        (self.work / "report.xlsx").write_bytes(b"main workbook bytes\x00\x01")
        (self.work / "marker.txt").write_bytes(b"PRIVATE MARKER MUST NOT APPEAR")
        (self.work / "worker").mkdir()
        (self.work / "worker" / "reviewer-messages.txt").write_text("private reviewer text")
        self.commit_and_push("main", "main")

        self.git("-C", str(self.work), "checkout", "-b", "archive/recovered")
        (self.work / "report.xlsx").write_bytes(b"historical workbook bytes\x02\x03")
        (self.work / "Backups").mkdir()
        (self.work / "Backups" / "Copy.XLSX").write_bytes(b"historical workbook bytes\x02\x03")
        (self.work / "notebook.xlsx").symlink_to("marker.txt")
        self.commit_and_push("archive", "archive/recovered")
        self.git("-C", str(self.work), "branch", "empty-branch")
        self.git("-C", str(self.work), "push", "origin", "empty-branch:refs/heads/empty-branch")

        # An uncommitted file cannot appear in any branch's Git tree.
        (self.work / "uncommitted.xlsx").write_bytes(b"never published")
        manifest = scan_public_repo(self.bare.as_uri(), self.output, allow_file_url=True)
        self.assertEqual(manifest["branches"], 3)
        self.assertEqual(len(manifest["files"]), 5)
        paths = {(item["ref"], item["path"]) for item in manifest["files"]}
        self.assertIn(("refs/heads/main", "report.xlsx"), paths)
        self.assertIn(("refs/heads/archive/recovered", "Backups/Copy.XLSX"), paths)
        self.assertNotIn(("refs/heads/archive/recovered", "notebook.xlsx"), paths)
        self.assertFalse(any("uncommitted" in path for _, path in paths))
        self.assertEqual(
            {item["ref"] for item in manifest["files"]},
            {"refs/heads/main", "refs/heads/archive/recovered", "refs/heads/empty-branch"},
        )
        self.assertEqual(
            {path.read_bytes() for path in (self.output / "blobs").glob("*.xlsx")},
            {b"main workbook bytes\x00\x01", b"historical workbook bytes\x02\x03"},
        )
        self.assertEqual(json.loads((self.output / "manifest.json").read_text()), manifest)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.output / "manifest.json").stat().st_mode), 0o600)
        self.assertFalse(any(p.name.startswith(".git-scan-") for p in self.output.iterdir()))
        self.assertFalse(any(b"PRIVATE MARKER" in p.read_bytes() for p in self.output.rglob("*.xlsx")))
        self.assertFalse(any("worker" in item["path"] for item in manifest["files"]))

    def test_scan_limits_fail_before_manifest_or_unbounded_blob_copy(self) -> None:
        (self.work / "first.xlsx").write_bytes(b"1234567890")
        self.commit_and_push("first", "main")
        self.git("-C", str(self.work), "checkout", "-b", "archive")
        (self.work / "second.xlsx").write_bytes(b"abcdefghij")
        self.commit_and_push("second", "archive")

        for label, changes in (
            ("branches", {"MAX_BRANCHES": 1}),
            ("files", {"MAX_FILES": 1}),
            ("blob", {"MAX_BLOB_BYTES": 9}),
            ("total", {"MAX_TOTAL_BYTES": 15}),
        ):
            output = self.root / f"bounded-{label}"
            with self.subTest(label=label), contextlib.ExitStack() as stack:
                for name, value in changes.items():
                    stack.enter_context(patch.object(attacker_read, name, value))
                with self.assertRaises(RuntimeError):
                    scan_public_repo(self.bare.as_uri(), output, allow_file_url=True)
                self.assertFalse((output / "manifest.json").exists())
                self.assertFalse(any((output / "blobs").glob(".part-*")))

    def test_rejects_credentials_and_local_url_without_test_opt_in(self) -> None:
        for url in (
            "https://name:password@example.com/owner/repo.git",
            "https://example.com/owner/repo.git?token=secret",
            self.bare.as_uri(),
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                scan_public_repo(url, self.output)
        self.assertFalse(self.output.exists())

    def test_empty_branch_and_no_spreadsheets(self) -> None:
        (self.work / "README.txt").write_text("public text")
        self.commit_and_push("no spreadsheets", "main")
        manifest = scan_public_repo(self.bare.as_uri(), self.output, allow_file_url=True)
        self.assertEqual(manifest["branches"], 1)
        self.assertEqual(manifest["files"], [])
        self.assertEqual(list((self.output / "blobs").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
