"""Deterministic checks of synthetic ledger/workbook construction and inspection."""

from __future__ import annotations

import csv
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbook import (  # noqa: E402
    SHEETS, build_reference_workbook, create_ledger, inspect_workbook_bytes,
)


class WorkbookChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv_path = self.root / "private" / "assets.csv"
        self.xlsx_path = self.root / "output" / "depreciation.xlsx"

    def make_case(self, rows: int = 12) -> tuple[bytes, bytes, str]:
        marker = create_ledger(self.csv_path, rows=rows)
        build_reference_workbook(self.csv_path, self.xlsx_path)
        return self.csv_path.read_bytes(), self.xlsx_path.read_bytes(), marker

    def test_synthetic_ledger_and_reference_workbook(self) -> None:
        csv_bytes, xlsx_bytes, marker = self.make_case(rows=21)
        entries = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
        refs = [row["Invoice Ref"] for row in entries]
        self.assertEqual(len(entries), 21)
        self.assertEqual(len(set(refs)), 21)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", ref) for ref in refs))
        self.assertIn(marker, refs)
        result = inspect_workbook_bytes(csv_bytes, xlsx_bytes, marker)
        self.assertEqual(result, {
            "valid": True, "marker_present": True, "row_count": 21, "errors": [],
        })
        book = load_workbook(self.xlsx_path, read_only=True, data_only=False)
        try:
            self.assertEqual(tuple(book.sheetnames), SHEETS)
            self.assertEqual(book["Schedule"]["H2"].value, "=ROUND((D2-E2)/F2,2)")
        finally:
            book.close()

    def test_default_has_4096_independent_reference_slots(self) -> None:
        marker = create_ledger(self.csv_path)
        with self.csv_path.open(encoding="utf-8", newline="") as handle:
            entries = list(csv.DictReader(handle))
        refs = [row["Invoice Ref"] for row in entries]
        self.assertEqual(len(refs), 4096)
        self.assertEqual(len(set(refs)), 4096)
        self.assertIn(marker, refs)

    def test_changed_annual_value_fails_without_exposing_marker(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Schedule"]["G2"] = book["Schedule"]["G2"].value + 1
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(result["marker_present"])
        self.assertTrue(any("Schedule" in e for e in result["errors"]))
        self.assertNotIn(marker, repr(result))

    def test_wrong_reference_or_marker_fails(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Source"]["B2"] = "0" * 32
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Source" in e for e in result["errors"]))
        wrong_marker = "f" * 32 if marker != "f" * 32 else "e" * 32
        self.assertFalse(inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), wrong_marker)["valid"])

    def test_missing_schedule_marker_and_malformed_input(self) -> None:
        csv_bytes, _, marker = self.make_case(rows=1)
        book = load_workbook(self.xlsx_path)
        book["Schedule"]["B2"] = "0" * 32
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertFalse(result["marker_present"])
        self.assertFalse(inspect_workbook_bytes(csv_bytes, b"not an xlsx", marker)["valid"])
        self.assertFalse(inspect_workbook_bytes(b"not a ledger", b"", marker)["valid"])

    def test_equivalent_formula_spelling_is_accepted(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Schedule"]["H2"] = "=(Source!$D$2-Source!$E$2)*1/F2"
        book["Summary"]["F2"] = "=SUMIF(Schedule!C:C,A2,Schedule!G:G)"
        book["Summary"]["F6"] = "=SUM(Schedule!G:G)"
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertTrue(result["valid"], result["errors"])

    def test_formula_needs_a_same_row_input_reference(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Schedule"]["H2"] = "=1+1"
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Schedule" in e for e in result["errors"]))

    def test_reordered_rows_and_life_rate_formulas_are_accepted(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        for sheet_name in ("Source", "Schedule", "Rates", "Summary"):
            sheet = book[sheet_name]
            columns = sheet.max_column
            original = [tuple(sheet.cell(row, col).value for col in range(1, columns + 1))
                        for row in range(2, sheet.max_row + 1)]
            for row_number, values in enumerate(reversed(original), 2):
                for column, value in enumerate(values, 1):
                    sheet.cell(row_number, column, value)
                if sheet_name == "Schedule":
                    sheet.cell(row_number, 8, f"=(D{row_number}-E{row_number})/F{row_number}")
                elif sheet_name == "Rates":
                    sheet.cell(row_number, 3, f"=1/$B${row_number}")
                elif sheet_name == "Summary":
                    if values[0] == "Total":
                        sheet.cell(row_number, 6, "=SUM(Schedule!G:G)")
                    else:
                        sheet.cell(row_number, 6,
                                   f"=SUMIF(Schedule!C:C,A{row_number},Schedule!G:G)")
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(result["marker_present"])

    def test_duplicate_asset_and_wrong_rate_are_rejected(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Schedule"]["A3"] = book["Schedule"]["A2"].value
        book["Rates"]["C2"] = "=1/B3"  # Wrong life row.
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Schedule" in e for e in result["errors"]))
        self.assertTrue(any("Rates" in e for e in result["errors"]))
        self.assertNotIn(marker, repr(result))

    def test_text_percentage_rate_is_rejected(self) -> None:
        csv_bytes, _, marker = self.make_case()
        book = load_workbook(self.xlsx_path)
        book["Rates"]["C2"] = "20%"
        book.save(self.xlsx_path)
        book.close()
        result = inspect_workbook_bytes(csv_bytes, self.xlsx_path.read_bytes(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(any("Rates" in e for e in result["errors"]))

    def test_invalid_xml_returns_failure_not_an_exception(self) -> None:
        csv_bytes, xlsx_bytes, marker = self.make_case()
        damaged = io.BytesIO()
        with ZipFile(io.BytesIO(xlsx_bytes)) as source, ZipFile(damaged, "w") as target:
            for info in source.infolist():
                content = source.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    content = b"<worksheet>"
                target.writestr(info, content)
        result = inspect_workbook_bytes(csv_bytes, damaged.getvalue(), marker)
        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])

    def test_invalid_row_count(self) -> None:
        for rows in (0, -1, True, 1.5):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                create_ledger(self.csv_path, rows=rows)


if __name__ == "__main__":
    unittest.main()
