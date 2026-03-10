"""Tests for position reconstruction: boundary balances, backward synthesis,
arithmetic closing balance, and fully-sold ticker handling.
"""
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.importers.trading212._models import T212Order
from opensteuerauszug.importers.trading212.trading212_importer import (
    Trading212Importer,
    _orders_to_mutation_stocks,
)

from .conftest import make_order, make_settings


# ---------------------------------------------------------------------------
# CSV mode: forward synthesis from zero
# ---------------------------------------------------------------------------

class TestPositionReconstruction:
    def test_boundary_balances_added(self):
        """Year-start and year-end mutation=False entries should be present."""
        order = make_order(side="BUY", filled_at=date(2024, 6, 1), quantity="10")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        mutation_stocks = _orders_to_mutation_stocks([order], "USD")
        all_stocks = importer._add_boundary_balances("TEST", mutation_stocks, "USD", current_position=None)
        balances = [s for s in all_stocks if not s.mutation]
        # At minimum year-start (Jan 1) and year-end (Jan 1 next year)
        assert len(balances) >= 2

    def test_position_zero_start_for_new_holding(self):
        """Asset bought within the tax year should have a zero opening balance."""
        order = make_order(side="BUY", filled_at=date(2024, 6, 1), quantity="10")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        mutation_stocks = _orders_to_mutation_stocks([order], "USD")
        all_stocks = importer._add_boundary_balances("TEST", mutation_stocks, "USD", current_position=None)
        start_balances = [
            s for s in all_stocks
            if not s.mutation and s.referenceDate == date(2024, 1, 1)
        ]
        assert len(start_balances) == 1
        assert start_balances[0].quantity == Decimal("0")

    def test_position_nonzero_start_for_prior_holding(self):
        """Asset bought before tax year should have non-zero opening balance."""
        prior_order = make_order(side="BUY", filled_at=date(2023, 7, 1), quantity="5")
        in_year_order = make_order(side="BUY", filled_at=date(2024, 3, 1), quantity="3")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        mutation_stocks = _orders_to_mutation_stocks([prior_order, in_year_order], "USD")
        all_stocks = importer._add_boundary_balances("TEST", mutation_stocks, "USD", current_position=None)
        start_balances = [
            s for s in all_stocks
            if not s.mutation and s.referenceDate == date(2024, 1, 1)
        ]
        assert len(start_balances) == 1
        assert start_balances[0].quantity == Decimal("5")


# ---------------------------------------------------------------------------
# Arithmetic closing balance
# ---------------------------------------------------------------------------

class TestArithmeticClosingBalance:
    def test_closing_equals_opening_plus_period_mutations(self):
        """Closing balance = opening balance + net buys/sells within the tax period."""
        pre_order = make_order(side="BUY", filled_at=date(2023, 6, 1), quantity="5")
        in_order = make_order(side="BUY", filled_at=date(2024, 4, 1), quantity="3")
        sell_order = make_order(side="SELL", filled_at=date(2024, 9, 1), quantity="1")

        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        mutation_stocks = _orders_to_mutation_stocks([pre_order, in_order, sell_order], "USD")
        all_stocks = importer._add_boundary_balances("TEST", mutation_stocks, "USD", current_position=None)

        balances = [s for s in all_stocks if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2024, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2025, 1, 1))

        assert opening.quantity == Decimal("5")   # pre-period buy
        assert closing.quantity == Decimal("7")   # 5 + 3 - 1


# ---------------------------------------------------------------------------
# API/hybrid mode: backward synthesis from live position
# ---------------------------------------------------------------------------

class TestApiModeBoundaryBalances:
    def test_api_mode_uses_current_position_as_anchor(self):
        """With a current_position, opening balance is synthesized backward from it."""
        order = make_order(side="BUY", filled_at=date(2024, 3, 1), quantity="10")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        mutation_stocks = _orders_to_mutation_stocks([order], "USD")
        current_position = {"ticker": "AAPL_US_EQ", "quantity": 10.0}
        all_stocks = importer._add_boundary_balances(
            "AAPL_US_EQ", mutation_stocks, "USD", current_position=current_position
        )
        # Should have opening and closing (today's anchor must NOT appear in output)
        balances = [s for s in all_stocks if not s.mutation]
        assert len(balances) >= 2
        start_balances = [s for s in balances if s.referenceDate == date(2024, 1, 1)]
        assert len(start_balances) == 1
        assert start_balances[0].quantity == Decimal("0")  # bought within year

    def test_recon_mutation_stocks_fixes_cross_year_backward_synthesis(self):
        """Hybrid mode prior-year: recon_mutation_stocks (full API history) must be
        used for synthesis, not just the CSV orders for the requested year.

        Scenario: stock bought in 2023 (+5), sold in 2024 (-5). CSV for 2023
        contains only 2023 mutations. Today's position = 0.

        Without fix: backward from 0 using only 2023 BUY (+5) → opening = -5,
        closing = 0. Wrong.

        With fix: backward from 0 using full history (+5 in 2023, -5 in 2024) →
        opening = 0, closing = 5. Correct.
        """
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2023, 1, 1),
            period_to=date(2023, 12, 31),
            account_settings_list=[settings],
        )

        # CSV orders: only 2023 BUY (as seen in t212-export-2023.csv)
        order_2023_buy = make_order(side="BUY", filled_at=date(2023, 6, 1), quantity="5")
        csv_mutation_stocks = _orders_to_mutation_stocks([order_2023_buy], "USD")

        # Full API orders: 2023 BUY + 2024 SELL (complete history)
        order_2024_sell = make_order(side="SELL", filled_at=date(2024, 3, 1), quantity="5")
        recon_mutation_stocks = _orders_to_mutation_stocks(
            [order_2023_buy, order_2024_sell], "USD"
        )

        # Today: stock fully sold, position = 0
        current_position = {"ticker": "AAPL_US_EQ", "quantity": 0}

        all_stocks = importer._add_boundary_balances(
            "AAPL_US_EQ",
            csv_mutation_stocks,
            "USD",
            current_position=current_position,
            recon_mutation_stocks=recon_mutation_stocks,
        )

        balances = [s for s in all_stocks if not s.mutation]
        opening = [s for s in balances if s.referenceDate == date(2023, 1, 1)]
        closing = [s for s in balances if s.referenceDate == date(2024, 1, 1)]

        assert len(opening) == 1, "opening balance must be present"
        assert opening[0].quantity == Decimal("0"), "no shares before 2023"
        assert len(closing) == 1, "closing balance must be present"
        assert closing[0].quantity == Decimal("5"), "5 shares held at end of 2023"

        # Today's position anchor must NOT appear in the output
        today_anchors = [s for s in balances if s.name and "Current Position" in s.name]
        assert len(today_anchors) == 0, "today's live position must not appear in output"


# ---------------------------------------------------------------------------
# Fully-sold tickers in hybrid/API mode (zero-anchor fix)
# ---------------------------------------------------------------------------

class TestFullySoldTickerHybridMode:
    """Tickers not in API current_positions should anchor at 0, not assume_zero."""

    def test_fully_sold_ticker_gets_zero_anchor(self):
        """A ticker sold before the API snapshot should get opening balance via 0-anchor."""
        pre_buy = make_order(side="BUY", filled_at=date(2024, 6, 1), quantity="100")
        in_sell = make_order(side="SELL", filled_at=date(2025, 1, 17), quantity="100")

        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2025, 1, 1),
            period_to=date(2025, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [pre_buy, in_sell], [], current_positions=[], instrument_types={},
        )

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2025, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2026, 1, 1))

        assert opening.quantity == Decimal("100")
        assert closing.quantity == Decimal("0")  # 100 - 100

    def test_fully_sold_ticker_no_negative_closing(self):
        """Partially sold ticker should not have negative closing when CSV is incomplete."""
        sell_order = make_order(side="SELL", filled_at=date(2025, 1, 17), quantity="100")

        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2025, 1, 1),
            period_to=date(2025, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [sell_order], [], current_positions=[], instrument_types={},
        )

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2025, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2026, 1, 1))

        assert opening.quantity == Decimal("100")
        assert closing.quantity == Decimal("0")  # 100 - 100

    def test_csv_only_mode_still_assumes_zero(self):
        """CSV-only mode (current_positions=None) should still use assume_zero."""
        sell_order = make_order(side="SELL", filled_at=date(2025, 1, 17), quantity="100")

        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2025, 1, 1),
            period_to=date(2025, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [sell_order], [], current_positions=None,
        )

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2025, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2026, 1, 1))

        # CSV-only: assume_zero → opening = 0, closing = 0 + (-100) = -100
        # This is the known CSV-only limitation
        assert opening.quantity == Decimal("0")
        assert closing.quantity == Decimal("-100")
