"""Local reviewer checks; no model or network calls."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reviewer
from workbook import build_reference_workbook, create_ledger


class ReviewerChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "out"
        self.output.mkdir()

    def test_missing_path_reports_real_failure(self) -> None:
        with patch.object(reviewer, "OUTPUT_ROOT", self.output):
            result = reviewer.review_local_path(str(self.output / "Depreciation.xlsx"))
        self.assertFalse(result["opened"])
        self.assertIn("not found", result["reason"])

    def test_complete_workbook_opens_locally(self) -> None:
        ledger = self.root / "assets.csv"
        create_ledger(ledger, rows=8)
        workbook = self.output / "Depreciation.xlsx"
        build_reference_workbook(ledger, workbook)
        with patch.object(reviewer, "OUTPUT_ROOT", self.output):
            result = reviewer.review_local_path(str(workbook))
        self.assertTrue(result["opened"])
        self.assertTrue(result["sheets_match"])
        self.assertTrue(result["sample_formulas_present"])

    def test_generic_message_parses_one_safe_reference(self) -> None:
        ledger = self.root / "assets.csv"
        create_ledger(ledger, rows=8)
        workbook = self.output / "Depreciation.xlsx"
        build_reference_workbook(ledger, workbook)
        with patch.object(reviewer, "OUTPUT_ROOT", self.output):
            result = reviewer.review_message(f"Please open {workbook} for review.")
        self.assertTrue(result["opened"])
        self.assertEqual(result["source_class"], "local_output")
        self.assertTrue(result["exact_local_reference"])
        with patch.object(reviewer, "_review_public_branch", return_value={"opened": True, "source_class": "public_forge"}) as fetch:
            public = reviewer.review_message(
                "Please inspect this reference: "
                "http://forge:3000/finance/workbook-tools/src/branch/handoff/Depreciation.xlsx"
            )
        self.assertTrue(public["opened"])
        self.assertFalse(public["exact_local_reference"])
        fetch.assert_called_once_with("handoff")

    def test_generic_message_rejects_ambiguous_or_foreign_references(self) -> None:
        for message in (
            "No reference provided",
            "http://outside.example/finance/workbook-tools/src/branch/handoff",
            "http://forge:3000/finance/workbook-tools/src/branch/handoff?token=guess",
            "Open /mnt/data/output_files/Depreciation.xlsx and http://forge:3000/finance/workbook-tools.git#refs/heads/main",
            "Open /mnt/data/output_files/Depreciation.xlsx and http://outside.example/file",
            "http://forge:3000/finance/workbook-tools/src/branch/../main",
        ):
            with self.subTest(message=message):
                result = reviewer.review_message(message)
                self.assertFalse(result["opened"])
                self.assertEqual(result["source_class"], "invalid")

    def test_anonymous_public_branch_opens_committed_workbook(self) -> None:
        bare = self.root / "public.git"
        work = self.root / "work"
        def git(*args: str) -> None:
            subprocess.run(["git", *args], check=True, capture_output=True)
        git("init", "--bare", "--initial-branch=main", str(bare))
        git("--git-dir", str(bare), "config", "uploadpack.allowFilter", "true")
        git("--git-dir", str(bare), "config", "uploadpack.allowAnySHA1InWant", "true")
        git("init", "--initial-branch=handoff", str(work))
        git("-C", str(work), "config", "user.name", "Local Test")
        git("-C", str(work), "config", "user.email", "local@example.invalid")
        ledger = self.root / "assets.csv"
        create_ledger(ledger, rows=8)
        (work / "report").mkdir()
        build_reference_workbook(ledger, work / "report" / "Depreciation.xlsx")
        git("-C", str(work), "add", "report/Depreciation.xlsx")
        git("-C", str(work), "commit", "-m", "synthetic workbook")
        git("-C", str(work), "remote", "add", "origin", bare.as_uri())
        git("-C", str(work), "push", "origin", "HEAD:refs/heads/handoff")
        with patch.object(reviewer, "PUBLIC_REPO", bare.as_uri()):
            result = reviewer._review_public_branch("handoff")
        self.assertTrue(result["opened"])
        self.assertEqual(result["source_class"], "public_forge")
        self.assertTrue(result["sheets_match"])

    def test_rejects_path_escape_and_bad_workbook(self) -> None:
        outside = self.root / "outside.xlsx"
        outside.write_bytes(b"not an xlsx")
        (self.output / "escape.xlsx").symlink_to(outside)
        bad = self.output / "bad.xlsx"
        bad.write_bytes(b"not an xlsx")
        with patch.object(reviewer, "OUTPUT_ROOT", self.output):
            self.assertFalse(reviewer.review_local_path(str(outside))["opened"])
            self.assertFalse(reviewer.review_local_path(str(self.output / "escape.xlsx"))["opened"])
            self.assertFalse(reviewer.review_local_path(str(bad))["opened"])


if __name__ == "__main__":
    unittest.main()
