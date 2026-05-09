"""Tests for CriticalWarning generation in the Trading212 importer."""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from opensteuerauszug.importers.trading212.api_client import T212ApiClient
from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer
from opensteuerauszug.model.critical_warning import CriticalWarning, CriticalWarningCategory

from .conftest import make_order, make_settings, write_csv


def _make_importer(**kwargs) -> Trading212Importer:
    return Trading212Importer(
        period_from=date(2024, 1, 1),
        period_to=date(2024, 12, 31),
        account_settings_list=[make_settings(**kwargs)],
    )


class TestNegativeBalanceWarning:
    """NEGATIVE_BALANCE warning when a SELL has no prior BUY in CSV mode."""

    def test_sell_without_buy_emits_negative_balance_warning(self):
        """A lone SELL order with no prior BUY produces a NEGATIVE_BALANCE warning."""
        importer = _make_importer()
        sell = make_order(side="SELL", filled_at=date(2024, 6, 1))
        stmt = importer._build_tax_statement([sell], [], current_positions=None)

        neg = [w for w in stmt.critical_warnings if w.category == CriticalWarningCategory.NEGATIVE_BALANCE]
        assert neg, "Expected a NEGATIVE_BALANCE warning for a SELL with no prior BUY"
        assert neg[0].identifier == sell.ticker

    def test_normal_buy_emits_no_warnings(self):
        """A simple BUY in CSV mode produces no CriticalWarnings."""
        importer = _make_importer()
        buy = make_order(side="BUY", filled_at=date(2024, 3, 1))
        stmt = importer._build_tax_statement([buy], [], current_positions=None)

        assert not stmt.critical_warnings, (
            f"Expected no warnings for a normal BUY, got: {stmt.critical_warnings}"
        )


class TestExtraWarningsPassthrough:
    """Warnings pre-built by _import_hybrid / _import_api reach the statement."""

    def test_extra_warnings_appear_on_statement(self):
        importer = _make_importer()
        sentinel = CriticalWarning(
            category=CriticalWarningCategory.OTHER,
            message="sentinel warning",
            source="test",
        )
        stmt = importer._build_tax_statement(
            [], [], current_positions=None, extra_warnings=[sentinel]
        )
        assert sentinel in stmt.critical_warnings


class TestHybridModeApiFailures:
    """Instrument/order API failures in hybrid mode produce CriticalWarnings."""

    _PATCH_PATH = "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient"

    def _run_hybrid(self, csv_path, mock_client):
        settings = make_settings(api_key="dummy-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with patch(self._PATCH_PATH, return_value=mock_client):
            return importer._import_hybrid(str(csv_path))

    def test_instrument_api_failure_produces_other_warning(self, tmp_path):
        """get_instruments_extended() failure → OTHER warning on statement."""
        csv_path = tmp_path / "t212.csv"
        write_csv(csv_path, [])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.side_effect = RuntimeError("network error")
        mock_client.get_current_positions.return_value = []
        mock_client.get_orders.return_value = []

        stmt = self._run_hybrid(csv_path, mock_client)

        other = [w for w in stmt.critical_warnings if w.category == CriticalWarningCategory.OTHER]
        assert other, "Expected an OTHER warning when instrument API fetch fails"
        assert "network error" in other[0].message

    def test_order_history_api_failure_produces_other_warning(self, tmp_path):
        """get_orders() failure in hybrid mode → OTHER warning on statement."""
        csv_path = tmp_path / "t212.csv"
        write_csv(csv_path, [])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = ({}, {})
        mock_client.get_current_positions.return_value = []
        mock_client.get_orders.side_effect = RuntimeError("timeout")

        stmt = self._run_hybrid(csv_path, mock_client)

        other = [w for w in stmt.critical_warnings if w.category == CriticalWarningCategory.OTHER]
        assert other, "Expected an OTHER warning when order history API fetch fails"
        assert "timeout" in other[0].message

    def test_both_api_failures_produce_two_warnings(self, tmp_path):
        """Both get_instruments_extended and get_orders failing → two OTHER warnings."""
        csv_path = tmp_path / "t212.csv"
        write_csv(csv_path, [])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.side_effect = RuntimeError("auth failed")
        mock_client.get_current_positions.return_value = []
        mock_client.get_orders.side_effect = RuntimeError("timeout")

        stmt = self._run_hybrid(csv_path, mock_client)

        other = [w for w in stmt.critical_warnings if w.category == CriticalWarningCategory.OTHER]
        assert len(other) >= 2, (
            f"Expected at least 2 OTHER warnings when both API calls fail, got {len(other)}"
        )
