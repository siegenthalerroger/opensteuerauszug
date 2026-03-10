"""Cross-year balance consistency validation using real CSV exports.

These are *optional integration tests* that run only when at least two annual CSV
exports are present in the project root directory.  They verify that the closing
balance of year N (derived from the merged full-history CSV) equals the opening
balance of year N+1 for every security that appears in both statements.

How it works
------------
Trading212 per-year CSV exports are merged into one combined order/dividend list
so that CSV-mode forward synthesis from zero has the complete account history.
The importer is then run once per tax year on this combined dataset and the
resulting boundary balances are compared across years.

Usage
-----
Place any number of ``t212-export-YYYY.csv`` (or ``t212-export-YYYY-*.csv``) files
in the project root, then run::

    pytest tests/importers/trading212/test_cross_year_validation.py -v

The tests are skipped automatically if fewer than two files are found.
Discovered files: determined at collection time by globbing the project root.
"""
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

logger = logging.getLogger(__name__)

from opensteuerauszug.importers.trading212.csv_parser import T212CsvParser
from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer
from opensteuerauszug.model.ech0196 import TaxStatement

from .conftest import make_settings

# ---------------------------------------------------------------------------
# File discovery (resolved at import/collection time)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _discover_csv_files() -> Dict[int, Path]:
    """Return {year: path} for all t212-export-YYYY*.csv files in project root."""
    result: Dict[int, Path] = {}
    for p in sorted(_PROJECT_ROOT.glob("t212-export-*.csv")):
        m = re.match(r"t212-export-(\d{4})", p.name)
        if m:
            year = int(m.group(1))
            result[year] = p  # last match wins if multiple files share a year
    return result


_CSV_FILES: Dict[int, Path] = _discover_csv_files()
_YEARS: List[int] = sorted(_CSV_FILES)
_YEAR_PAIRS: List[Tuple[int, int]] = list(zip(_YEARS, _YEARS[1:]))

pytestmark = pytest.mark.skipif(
    len(_YEARS) < 2,
    reason=(
        "Cross-year validation requires at least two t212-export-YYYY*.csv files "
        f"in the project root ({_PROJECT_ROOT}). "
        "Place the files there and re-run to enable these tests."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_all_orders_and_dividends():
    """Parse all discovered CSV files and return merged (orders, dividends) lists."""
    all_orders, all_dividends = [], []
    logger.info("Loading %d CSV export(s):", len(_CSV_FILES))
    for year, csv_path in _CSV_FILES.items():
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        logger.info("  %d: %s — %d order(s), %d dividend(s)", year, csv_path.name, len(orders), len(dividends))
        all_orders.extend(orders)
        all_dividends.extend(dividends)
    logger.info("Merged total: %d order(s), %d dividend(s)", len(all_orders), len(all_dividends))
    return all_orders, all_dividends


def _run_year(all_orders, all_dividends, year: int) -> TaxStatement:
    """Run the importer for *year* using the merged order/dividend history."""
    settings = make_settings()
    importer = Trading212Importer(
        period_from=date(year, 1, 1),
        period_to=date(year, 12, 31),
        account_settings_list=[settings],
    )
    # CSV mode (current_positions=None): uses assume_zero + forward synthesis.
    # This is accurate when all_orders covers the full account history.
    return importer._build_tax_statement(
        all_orders, all_dividends, current_positions=None
    )


def _balance_by_ticker(stmt: TaxStatement, balance_name: str) -> Dict[str, Decimal]:
    """Return {ticker: qty} for all balances whose name starts with *balance_name*."""
    result: Dict[str, Decimal] = {}
    if stmt.listOfSecurities is None:
        return result
    for depot in stmt.listOfSecurities.depot:
        for sec in depot.security:
            matches = [
                s for s in sec.stock
                if not s.mutation and (s.name or "").startswith(balance_name)
            ]
            if matches:
                result[sec.symbol] = matches[0].quantity
    return result


def _net_mutations_up_to(all_orders, ticker: str, before_date: date) -> Decimal:
    """Net buy/sell quantity for *ticker* from orders with filled_at < *before_date*.

    For forward synthesis from zero, the balance at *before_date* equals exactly
    this value.  A negative result means more sells than buys are visible in the
    CSV — i.e. a pre-period data gap, not a synthesis error.
    """
    net = Decimal(0)
    for order in all_orders:
        if order.ticker == ticker and order.filled_at < before_date:
            net += order.quantity if order.side == "BUY" else -order.quantity
    return net


def _mutation_detail(all_orders, ticker: str, before_date: date) -> str:
    """Return a human-readable breakdown of all orders for *ticker* up to *before_date*.

    Shows each order on its own line (date, side, quantity, running total) so that
    the xfail reason is fully self-contained and examinable without re-running.
    """
    relevant = sorted(
        (o for o in all_orders if o.ticker == ticker and o.filled_at < before_date),
        key=lambda o: o.filled_at,
    )
    if not relevant:
        return "    (no orders found before this date)"

    lines = []
    running = Decimal(0)
    for o in relevant:
        delta = o.quantity if o.side == "BUY" else -o.quantity
        running += delta
        lines.append(
            f"    {o.filled_at}  {o.side:4s}  {delta:+.6f}  running={running:.6f}"
        )
    total_buys = sum(o.quantity for o in relevant if o.side == "BUY")
    total_sells = sum(o.quantity for o in relevant if o.side != "BUY")
    lines.append(
        f"    — {len(relevant)} order(s): buys={total_buys:.6f}, sells={total_sells:.6f}, net={running:.6f}"
    )
    return "\n".join(lines)


def _check_transition(
    closing: Dict[str, Decimal],
    opening: Dict[str, Decimal],
    year_from: int,
    year_to: int,
) -> None:
    """Assert closing[year_from] == opening[year_to] for all shared tickers."""
    common = set(closing) & set(opening)
    assert common, (
        f"No securities appear in both {year_from} and {year_to} statements — "
        "the CSV exports may not overlap."
    )
    discrepancies: Dict[str, Tuple[Decimal, Decimal]] = {}
    for ticker in sorted(common):
        c, o = closing[ticker], opening[ticker]
        if c != o:
            discrepancies[ticker] = (c, o)

    assert not discrepancies, (
        f"Closing {year_from} ≠ Opening {year_to} for {len(discrepancies)} ticker(s):\n"
        + "\n".join(
            f"  {t}: closing={c}, opening={o}"
            for t, (c, o) in discrepancies.items()
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCrossYearBalanceConsistency:
    """Closing balance of year N must equal opening balance of year N+1.

    All discovered CSV exports are merged so that forward synthesis from zero has
    the full account history.  Any discrepancy indicates a position
    reconstruction bug or a gap/duplicate in the CSV exports.

    Discovered years: determined dynamically from t212-export-YYYY*.csv files in
    the project root.
    """

    @pytest.fixture(scope="class")
    def merged_data(self):
        """Parse all discovered CSV exports once and cache the result for the class."""
        return _load_all_orders_and_dividends()

    @pytest.fixture(scope="class")
    def statements(self, merged_data):
        all_orders, all_dividends = merged_data
        return {
            year: _run_year(all_orders, all_dividends, year)
            for year in _YEARS
        }

    @pytest.mark.parametrize("year_from,year_to", _YEAR_PAIRS)
    def test_closing_equals_next_opening(self, statements, year_from, year_to):
        """Closing balance at 31.12.{year_from} must equal opening balance at 01.01.{year_to}."""
        closing = _balance_by_ticker(statements[year_from], "Closing")
        opening = _balance_by_ticker(statements[year_to], "Opening")
        _check_transition(closing, opening, year_from, year_to)

    def test_zero_or_positive_quantities_only(self, statements, merged_data):
        """No security should have a negative opening or closing balance in any year.

        For each negative balance we check whether it is a *data gap* or a
        *synthesis bug*:

        Data gap (xfail)
            For CSV forward-synthesis-from-zero, balance = sum(mutations).
            If the net of all orders for that ticker up to the balance date is
            also negative, the CSV simply lacks the earlier BUY records — the
            synthesis computed the correct answer given its inputs.

        Synthesis bug (hard fail)
            If net_mutations ≥ 0 but the synthesised balance is negative, the
            importer produced a wrong result despite having enough data.  This
            indicates a real bug (e.g. misattributed ticker, double-counted sell).
        """
        all_orders, _ = merged_data
        data_gaps: list = []
        synthesis_bugs: list = []
        # Track tickers already reported to avoid repeating the same root cause
        # across multiple years (a persistent negative balance shows up in every
        # subsequent opening and closing balance).
        seen_gap_tickers: set = set()
        seen_bug_tickers: set = set()

        for year, stmt in sorted(statements.items()):
            if stmt.listOfSecurities is None:
                continue
            for depot in stmt.listOfSecurities.depot:
                for sec in depot.security:
                    for s in sec.stock:
                        if not s.mutation and s.quantity < 0:
                            ticker = sec.symbol
                            net = _net_mutations_up_to(
                                all_orders, ticker, s.referenceDate
                            )
                            if net < 0:
                                if ticker not in seen_gap_tickers:
                                    seen_gap_tickers.add(ticker)
                                    detail = _mutation_detail(
                                        all_orders, ticker, s.referenceDate
                                    )
                                    data_gaps.append(
                                        f"  {ticker}: first negative in {year} "
                                        f"{s.name} = {s.quantity} "
                                        f"(net_mutations={net}, ref={s.referenceDate})\n"
                                        f"{detail}"
                                    )
                            else:
                                if ticker not in seen_bug_tickers:
                                    seen_bug_tickers.add(ticker)
                                    detail = _mutation_detail(
                                        all_orders, ticker, s.referenceDate
                                    )
                                    synthesis_bugs.append(
                                        f"  {ticker}: first negative in {year} "
                                        f"{s.name} = {s.quantity} "
                                        f"(net_mutations={net}, ref={s.referenceDate})\n"
                                        f"{detail}"
                                    )

        assert not synthesis_bugs, (
            f"Synthesis bug(s) detected — negative balance despite non-negative "
            f"net mutations in the CSV exports:\n" + "\n".join(synthesis_bugs)
        )

        if data_gaps:
            pytest.xfail(
                f"{len(data_gaps)} ticker(s) with negative balance confirmed as data gaps — "
                f"net mutations in the merged CSV are also negative, so BUY "
                f"history predates the earliest export.  Supply a full-history "
                f"CSV from account inception to resolve:\n"
                + "\n".join(data_gaps)
            )
