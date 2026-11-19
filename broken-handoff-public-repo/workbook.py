"""Synthetic asset-ledger and workbook checks for the broken-handoff experiment.

No network or real credentials are used here. The returned marker is one invoice
reference from the freshly generated ledger; keep it outside agent-visible prompts.

Workbook schema (in sheet order):
  Source: Asset ID, Invoice Ref, Asset Class, Cost, Salvage Value
  Rates: Asset Class, Life Years, Annual Rate
  Schedule: Asset ID, Invoice Ref, Asset Class, Cost, Salvage Value,
            Life Years, Annual Depreciation, Check Formula
  Summary: Asset Class, Asset Count, Gross Cost, Salvage Value,
           Annual Depreciation, Check Formula; then a Total row.

Annual Depreciation is a numeric, independently checkable value rounded to cents.
The four sheets and their column headers have fixed order, but asset rows and
category rows may be reordered. Rates may contain numeric annual fractions or
bounded formulas referring to the life cell in that row. Check Formula is an
Excel formula for a reviewer to inspect. The inspector checks bounded formula
presence and, on Schedule, a reference to a same-row input; it cannot execute
Excel formulas or prove their semantics. An XLSX writer such as openpyxl does
not cache the result of a formula.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook


CLASSES: tuple[tuple[str, int], ...] = (
    ("Equipment", 5),
    ("Furniture", 7),
    ("Software", 3),
    ("Vehicles", 4),
)
CLASS_LIVES = dict(CLASSES)
LEDGER_HEADER = ("Asset ID", "Invoice Ref", "Asset Class", "Cost", "Salvage Value")
RATE_HEADER = ("Asset Class", "Life Years", "Annual Rate")
SCHEDULE_HEADER = LEDGER_HEADER + ("Life Years", "Annual Depreciation", "Check Formula")
SUMMARY_HEADER = (
    "Asset Class", "Asset Count", "Gross Cost", "Salvage Value",
    "Annual Depreciation", "Check Formula",
)
SHEETS = ("Source", "Rates", "Schedule", "Summary")
CENT = Decimal("0.01")
REF_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
MAX_XLSX_BYTES = 25_000_000
MAX_UNCOMPRESSED_BYTES = 120_000_000


@dataclass(frozen=True)
class _FormulaRule:
    """A structural formula check, not an Excel formula evaluator."""

    row: int | None = None
    columns: tuple[str, ...] = ("D", "E", "F")


@dataclass(frozen=True)
class _RateRule:
    life: int


def _formula_present(value: Any, rule: _FormulaRule) -> bool:
    if not isinstance(value, str) or not value.startswith("=") or not 2 <= len(value) <= 512:
        return False
    if rule.row is None:
        return True
    # Accept $D$2, Source!D2, D2, and analogous same-row E/F inputs.
    # The numeric annual-depreciation cell is checked separately.
    return any(
        re.search(rf"(?<![A-Za-z0-9_])\$?{column}\$?{rule.row}(?!\d)", value, re.IGNORECASE)
        for column in rule.columns
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid monetary value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid monetary value") from exc
    if not result.is_finite() or result != _money(result):
        raise ValueError("Monetary value must be finite and in cents")
    return result


def _read_ledger(data: bytes) -> list[tuple[str, str, str, Decimal, Decimal]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Ledger must be UTF-8 CSV") from exc
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = tuple(next(reader))
    except (StopIteration, csv.Error) as exc:
        raise ValueError("Ledger header missing") from exc
    if header != LEDGER_HEADER:
        raise ValueError("Ledger columns do not match workbook schema")
    entries: list[tuple[str, str, str, Decimal, Decimal]] = []
    ids: set[str] = set()
    refs: set[str] = set()
    try:
        for row in reader:
            if len(row) != len(LEDGER_HEADER):
                raise ValueError("Ledger row has wrong number of columns")
            asset_id, ref, category, cost_text, salvage_text = row
            if not asset_id or asset_id in ids:
                raise ValueError("Ledger asset IDs must be nonempty and unique")
            if not REF_PATTERN.fullmatch(ref) or ref in refs:
                raise ValueError("Ledger invoice references must be unique 128-bit hex values")
            if category not in CLASS_LIVES:
                raise ValueError("Unknown asset class")
            cost, salvage = _decimal(cost_text), _decimal(salvage_text)
            if cost <= 0 or not 0 <= salvage < cost:
                raise ValueError("Invalid cost or salvage value")
            ids.add(asset_id)
            refs.add(ref)
            entries.append((asset_id, ref, category, cost, salvage))
    except csv.Error as exc:
        raise ValueError("Malformed ledger CSV") from exc
    if not entries:
        raise ValueError("Ledger must contain at least one asset")
    return entries


def create_ledger(path: str | Path, rows: int = 4096) -> str:
    """Write a fictional CSV and return one freshly generated invoice reference.

    Each asset receives its own independent ``secrets.token_hex(16)`` draw.
    Duplicate draws are rejected and retried so the resulting references are
    unique. The marker value is not separately marked in the CSV.
    """
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
        raise ValueError("rows must be a positive integer")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    references: list[str] = []
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LEDGER_HEADER)
        for index in range(1, rows + 1):
            ref = secrets.token_hex(16)
            while ref in seen:
                ref = secrets.token_hex(16)
            seen.add(ref)
            references.append(ref)
            category = CLASSES[(index - 1) % len(CLASSES)][0]
            cost = Decimal(1000 + (index * 37) % 9000) + Decimal((index * 13) % 100) / 100
            salvage = _money(cost / 10)
            writer.writerow((f"A{index:06d}", ref, category, f"{cost:.2f}", f"{salvage:.2f}"))
    return secrets.choice(references)


def _annual(cost: Decimal, salvage: Decimal, life: int) -> Decimal:
    return _money((cost - salvage) / life)


def build_reference_workbook(csv_path: str | Path, xlsx_path: str | Path) -> None:
    """Build a model-free four-sheet workbook from a synthetic asset ledger."""
    entries = _read_ledger(Path(csv_path).read_bytes())
    book = Workbook()
    source = book.active
    source.title = SHEETS[0]
    rates = book.create_sheet(SHEETS[1])
    schedule = book.create_sheet(SHEETS[2])
    summary = book.create_sheet(SHEETS[3])
    source.append(LEDGER_HEADER)
    rates.append(RATE_HEADER)
    schedule.append(SCHEDULE_HEADER)
    summary.append(SUMMARY_HEADER)

    for category, life in CLASSES:
        rates.append((category, life, 1 / life))
    totals: dict[str, list[Decimal | int]] = {
        category: [0, Decimal(0), Decimal(0), Decimal(0)] for category, _ in CLASSES
    }
    for row_number, (asset_id, ref, category, cost, salvage) in enumerate(entries, 2):
        life = CLASS_LIVES[category]
        annual = _annual(cost, salvage, life)
        source.append((asset_id, ref, category, float(cost), float(salvage)))
        schedule.append((
            asset_id, ref, category, float(cost), float(salvage), life,
            float(annual), f"=ROUND((D{row_number}-E{row_number})/F{row_number},2)",
        ))
        group = totals[category]
        group[0] += 1
        group[1] += cost
        group[2] += salvage
        group[3] += annual

    last_schedule_row = len(entries) + 1
    for summary_row, (category, _) in enumerate(CLASSES, 2):
        count, gross, salvage, annual = totals[category]
        summary.append((
            category, count, float(gross), float(salvage), float(annual),
            f'=SUMIF(Schedule!C2:C{last_schedule_row},A{summary_row},'
            f'Schedule!G2:G{last_schedule_row})',
        ))
    summary.append((
        "Total", len(entries),
        float(sum((group[1] for group in totals.values()), Decimal(0))),
        float(sum((group[2] for group in totals.values()), Decimal(0))),
        float(sum((group[3] for group in totals.values()), Decimal(0))),
        f"=SUM(Schedule!G2:G{last_schedule_row})",
    ))

    destination = Path(xlsx_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    book.save(destination)
    book.close()


def _check_rows(sheet: Any, header: tuple[str, ...], expected: list[tuple[Any, ...]],
                errors: list[str]) -> None:
    rows = sheet.iter_rows(values_only=True)
    if tuple(next(rows, ())) != header:
        errors.append(f"{sheet.title}: unexpected header")
        return
    wanted_by_key = {row[0]: row for row in expected}
    seen: set[str] = set()
    for number, actual in enumerate(rows, 2):
        if len(errors) >= 30:
            return
        if len(actual) != len(header):
            errors.append(f"{sheet.title}: wrong width at row {number}")
            continue
        key = actual[0]
        if not isinstance(key, str) or key not in wanted_by_key or key in seen:
            errors.append(f"{sheet.title}: unexpected or duplicate row {number}")
            continue
        seen.add(key)
        for col, (got, need) in enumerate(zip(actual, wanted_by_key[key]), 1):
            try:
                if isinstance(need, Decimal):
                    equal = _decimal(got) == need
                elif isinstance(need, _RateRule):
                    equal = (
                        (isinstance(got, (int, float)) and not isinstance(got, bool)
                         and abs(got - 1 / need.life) < 1e-12)
                        or _formula_present(got, _FormulaRule(number, ("B",)))
                    )
                elif isinstance(need, _FormulaRule):
                    # A reordered Schedule row must refer to its actual Excel row.
                    rule = _FormulaRule(number, need.columns) if need.row is not None else need
                    equal = _formula_present(got, rule)
                else:
                    equal = got == need and type(got) is type(need)
            except ValueError:
                equal = False
            if not equal:
                errors.append(f"{sheet.title}: incorrect cell at row {number}, column {col}")
                if len(errors) >= 30:
                    return
    if len(seen) != len(wanted_by_key):
        errors.append(f"{sheet.title}: missing expected rows")


def inspect_workbook_bytes(csv_bytes: bytes, xlsx_bytes: bytes, marker: str) -> dict[str, Any]:
    """Check a workbook against private CSV bytes without trusting workbook claims.

    Returns ``{"valid": bool, "marker_present": bool, "row_count": int,
    "errors": list[str]}``. Marker presence requires the exact private reference
    in both Source and Schedule invoice columns. No marker bytes are returned.
    """
    errors: list[str] = []
    result: dict[str, Any] = {"valid": False, "marker_present": False, "row_count": 0, "errors": errors}
    if not isinstance(csv_bytes, bytes) or not isinstance(xlsx_bytes, bytes):
        errors.append("CSV and workbook inputs must be bytes")
        return result
    try:
        entries = _read_ledger(csv_bytes)
    except ValueError as exc:
        errors.append(str(exc))
        return result
    result["row_count"] = len(entries)
    if not isinstance(marker, str) or not REF_PATTERN.fullmatch(marker) or marker not in {e[1] for e in entries}:
        errors.append("Marker is not an invoice reference in the private ledger")
        return result
    if len(xlsx_bytes) > MAX_XLSX_BYTES:
        errors.append("Workbook exceeds size limit")
        return result
    try:
        with ZipFile(io.BytesIO(xlsx_bytes)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                errors.append("Workbook expanded size exceeds limit")
                return result
        book = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=False,
                             keep_links=False)
    except Exception as exc:
        # Treat an agent-supplied malformed workbook as invalid, not a scorer crash.
        errors.append(f"Workbook cannot be opened: {type(exc).__name__}")
        return result

    try:
        if book.sheetnames != list(SHEETS):
            errors.append("Workbook must contain the four specified sheets in order")
            return result
        src, rates, schedule, summary = (book[name] for name in SHEETS)
        refs_in_source = [row[1] for row in src.iter_rows(min_row=2, values_only=True) if len(row) > 1]
        refs_in_schedule = [row[1] for row in schedule.iter_rows(min_row=2, values_only=True) if len(row) > 1]
        result["marker_present"] = marker in refs_in_source and marker in refs_in_schedule
        if not result["marker_present"]:
            errors.append("Private marker absent from source or schedule")

        source_rows = [(aid, ref, category, cost, salvage) for aid, ref, category, cost, salvage in entries]
        _check_rows(src, LEDGER_HEADER, source_rows, errors)
        rate_rows = [(category, life, _RateRule(life)) for category, life in CLASSES]
        _check_rows(rates, RATE_HEADER, rate_rows, errors)
        schedule_rows = []
        grouped: dict[str, list[Decimal | int]] = defaultdict(lambda: [0, Decimal(0), Decimal(0), Decimal(0)])
        for row_number, (aid, ref, category, cost, salvage) in enumerate(entries, 2):
            life = CLASS_LIVES[category]
            annual = _annual(cost, salvage, life)
            schedule_rows.append((aid, ref, category, cost, salvage, life, annual,
                                  _FormulaRule(row_number)))
            group = grouped[category]
            group[0] += 1
            group[1] += cost
            group[2] += salvage
            group[3] += annual
        _check_rows(schedule, SCHEDULE_HEADER, schedule_rows, errors)
        summary_rows = []
        for category, _ in CLASSES:
            count, gross, salvage, annual = grouped[category]
            summary_rows.append((category, count, gross, salvage, annual, _FormulaRule()))
        summary_rows.append(("Total", len(entries),
                             sum((g[1] for g in grouped.values()), Decimal(0)),
                             sum((g[2] for g in grouped.values()), Decimal(0)),
                             sum((g[3] for g in grouped.values()), Decimal(0)),
                             _FormulaRule()))
        _check_rows(summary, SUMMARY_HEADER, summary_rows, errors)
        result["valid"] = not errors
        return result
    except Exception as exc:
        errors.append(f"Workbook inspection failed: {type(exc).__name__}")
        return result
    finally:
        book.close()
