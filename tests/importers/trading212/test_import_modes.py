"""Tests for Trading212Importer mode detection, CSV import, hybrid mode,
crypto filtering, and ticker remapping.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opensteuerauszug.importers.trading212.api_client import T212ApiClient
from opensteuerauszug.importers.trading212.trading212_importer import (
    Trading212Importer,
    _remap_tickers_via_isin,
)

from .conftest import make_dividend, make_order, make_settings, write_csv


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

class TestModeDetection:
    def test_csv_mode_detected_for_file(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
            "Currency (Price / share),Exchange rate,Total,Currency (Total)\n"
            "Market buy,2024-03-15 10:00:00,US0378331005,AAPL_US_EQ,Apple Inc,"
            "10,150.00,USD,0.92,1380.00,EUR\n"
        )
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with patch.object(importer, "_import_csv", return_value=MagicMock()) as mock_csv:
            importer.import_from(str(csv_file))
            mock_csv.assert_called_once_with(str(csv_file))

    def test_api_mode_detected_for_directory(self, tmp_path):
        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with patch.object(importer, "_import_api", return_value=MagicMock()) as mock_api:
            importer.import_from(str(tmp_path))
            mock_api.assert_called_once()

    def test_invalid_path_raises(self):
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with pytest.raises(ValueError, match="must be an existing file"):
            importer.import_from("/nonexistent/path/file.csv")

    def test_api_mode_without_key_raises(self, tmp_path):
        settings = make_settings(api_key=None)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with pytest.raises(ValueError, match="api_key"):
            importer._import_api()


# ---------------------------------------------------------------------------
# CSV import (end-to-end)
# ---------------------------------------------------------------------------

class TestCsvImport:
    def test_csv_import_produces_tax_statement(self, tmp_path):
        csv_path = tmp_path / "t212.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "10", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
            {
                "Action": "Dividend", "Time": "2024-05-16 00:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "10", "Price / share": "0.25",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "2.30", "Currency (Total)": "EUR",
            },
        ])
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        statement = importer.import_from(str(csv_path))
        assert statement.listOfSecurities is not None
        depots = statement.listOfSecurities.depot
        assert len(depots) == 1
        securities = depots[0].security
        assert len(securities) == 1
        sec = securities[0]
        assert sec.symbol == "AAPL_US_EQ"
        assert len(sec.payment) == 1

    def test_csv_institution_name(self, tmp_path):
        csv_path = tmp_path / "t212.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "5", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "690.00", "Currency (Total)": "EUR",
            },
        ])
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        statement = importer.import_from(str(csv_path))
        assert statement.institution is not None
        assert statement.institution.name == "Trading212"


# ---------------------------------------------------------------------------
# Crypto filtering
# ---------------------------------------------------------------------------

class TestCryptoFiltering:
    def test_crypto_skipped_when_ignore_crypto_true(self, monkeypatch):
        """Crypto tickers should not appear in output when ignore_crypto=True."""
        order = make_order(ticker="BTC_EQ", isin=None, name="Bitcoin")
        settings = make_settings(ignore_crypto=True)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        monkeypatch.setattr(
            "opensteuerauszug.importers.trading212.trading212_importer._extract_instrument_info",
            lambda ticker, orders, dividends, sec_type_override=None: (None, ticker, "CRYPTO", "USD"),
        )
        stmt = importer._build_tax_statement([order], [], current_positions=None)

        assert stmt.listOfSecurities is None

    def test_crypto_included_when_ignore_crypto_false(self, monkeypatch):
        """Crypto tickers should appear in output when ignore_crypto=False."""
        order = make_order(ticker="BTC_EQ", isin=None, name="Bitcoin")
        settings = make_settings(ignore_crypto=False)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        monkeypatch.setattr(
            "opensteuerauszug.importers.trading212.trading212_importer._extract_instrument_info",
            lambda ticker, orders, dividends, sec_type_override=None: (None, ticker, "CRYPTO", "USD"),
        )
        stmt = importer._build_tax_statement([order], [], current_positions=None)

        assert stmt.listOfSecurities is not None
        assert len(stmt.listOfSecurities.depot[0].security) == 1


# ---------------------------------------------------------------------------
# Hybrid mode (CSV + API)
# ---------------------------------------------------------------------------

class TestHybridMode:
    """Tests for the hybrid CSV+API mode."""

    def test_hybrid_mode_selected_when_file_and_api_key(self, tmp_path):
        """import_from() dispatches to _import_hybrid when file + api_key."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
            "Currency (Price / share),Exchange rate,Total,Currency (Total)\n"
        )
        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with patch.object(importer, "_import_hybrid", return_value=MagicMock()) as mock:
            importer.import_from(str(csv_file))
            mock.assert_called_once_with(str(csv_file))

    def test_csv_mode_when_file_and_no_api_key(self, tmp_path):
        """import_from() falls back to CSV mode when no api_key configured."""
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
            "Currency (Price / share),Exchange rate,Total,Currency (Total)\n"
        )
        settings = make_settings(api_key=None)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        with patch.object(importer, "_import_csv", return_value=MagicMock()) as mock:
            importer.import_from(str(csv_file))
            mock.assert_called_once_with(str(csv_file))

    def test_hybrid_preserves_csv_wht_and_fx(self, tmp_path):
        """Hybrid mode keeps withholding tax and FX rates from CSV dividends."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Dividend", "Time": "2024-05-16 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "10", "Price / share": "0.25",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "2.30", "Currency (Total)": "EUR",
                "Withholding tax": "0.38", "Currency (Withholding tax)": "EUR",
            },
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "10", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"AAPL_US_EQ": "STOCK"},
            {"US0378331005": "AAPL_US_EQ"},
        )
        mock_client.get_current_positions.return_value = [
            {"ticker": "AAPL_US_EQ", "quantity": 10},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        sec = stmt.listOfSecurities.depot[0].security[0]
        assert len(sec.payment) == 1
        pmt = sec.payment[0]
        assert pmt.withHoldingTaxClaim == Decimal("0.38")
        assert pmt.exchangeRate == Decimal("0.92")

    def test_hybrid_uses_api_instrument_types(self, tmp_path):
        """Hybrid mode maps instrument types from the API, not defaulting to STOCK."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "IE00B4L5Y983", "Ticker": "VWRL_EQ",
                "Name": "Vanguard FTSE All-World",
                "No. of shares": "5", "Price / share": "100.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "460.00", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"VWRL_EQ": "ETF"},
            {"IE00B4L5Y983": "VWRL_EQ"},
        )
        mock_client.get_current_positions.return_value = [
            {"ticker": "VWRL_EQ", "quantity": 5},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.securityCategory == "FUND"  # ETF → FUND mapping

    def test_hybrid_filters_crypto_via_api_types(self, tmp_path):
        """Hybrid mode filters crypto instruments using API instrument types."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "CRYPTO123456", "Ticker": "BTC_CRYPTO", "Name": "Bitcoin",
                "No. of shares": "1", "Price / share": "50000.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "46000.00", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"BTC_CRYPTO": "CRYPTOCURRENCY"},
            {"CRYPTO123456": "BTC_CRYPTO"},
        )
        mock_client.get_current_positions.return_value = []

        settings = make_settings(api_key="test-key", ignore_crypto=True)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        assert stmt.listOfSecurities is None

    def test_hybrid_instruments_fetch_failure_falls_back(self, tmp_path):
        """Hybrid mode still works if get_instruments() raises; defaults to STOCK."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "10", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.side_effect = RuntimeError("API error")
        mock_client.get_current_positions.return_value = [
            {"ticker": "AAPL_US_EQ", "quantity": 10},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.securityCategory == "SHARE"  # Fell back to STOCK → SHARE

    def test_hybrid_uses_api_positions_for_balances(self, tmp_path):
        """Hybrid mode uses API current positions for backward synthesis."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2023-06-01 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "5", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "690.00", "Currency (Total)": "EUR",
            },
            {
                "Action": "Market buy", "Time": "2024-04-01 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple Inc",
                "No. of shares": "3", "Price / share": "170.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.93",
                "Total": "474.30", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"AAPL_US_EQ": "STOCK"},
            {"US0378331005": "AAPL_US_EQ"},
        )
        # API reports 8 shares held today (5 + 3)
        mock_client.get_current_positions.return_value = [
            {"ticker": "AAPL_US_EQ", "quantity": 8},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2024, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2025, 1, 1))

        assert opening.quantity == Decimal("5")  # backward synthesis from API anchor
        assert closing.quantity == Decimal("8")   # 5 + 3


# ---------------------------------------------------------------------------
# Ticker remapping (CSV short tickers → API qualified tickers)
# ---------------------------------------------------------------------------

class TestTickerRemapping:
    """CSV exports use short tickers (e.g. INTC), API uses qualified (INTC_US_EQ)."""

    def test_remap_orders_and_dividends_via_isin(self):
        """_remap_tickers_via_isin bridges CSV short tickers to API tickers."""
        orders = [
            make_order(ticker="INTC", isin="US4581401001"),
            make_order(ticker="AAPL", isin="US0378331005"),
        ]
        dividends = [
            make_dividend(ticker="INTC", isin="US4581401001"),
        ]
        isin_map = {
            "US4581401001": "INTC_US_EQ",
            "US0378331005": "AAPL_US_EQ",
        }
        _remap_tickers_via_isin(orders, dividends, isin_map)

        assert orders[0].ticker == "INTC_US_EQ"
        assert orders[1].ticker == "AAPL_US_EQ"
        assert dividends[0].ticker == "INTC_US_EQ"

    def test_remap_skips_orders_without_isin(self):
        """Orders without ISIN keep their original ticker."""
        orders = [make_order(ticker="MYSTERY", isin=None)]
        _remap_tickers_via_isin(orders, [], {"US0378331005": "AAPL_US_EQ"})
        assert orders[0].ticker == "MYSTERY"

    def test_remap_skips_unknown_isin(self):
        """Orders whose ISIN is not in the map keep their original ticker."""
        orders = [make_order(ticker="INTC", isin="US4581401001")]
        _remap_tickers_via_isin(orders, [], {"US0378331005": "AAPL_US_EQ"})
        assert orders[0].ticker == "INTC"  # unchanged

    def test_remap_no_change_when_already_matching(self):
        """If CSV ticker already matches API ticker, no remap needed."""
        orders = [make_order(ticker="AAPL_US_EQ", isin="US0378331005")]
        _remap_tickers_via_isin(orders, [], {"US0378331005": "AAPL_US_EQ"})
        assert orders[0].ticker == "AAPL_US_EQ"

    def test_hybrid_remaps_csv_tickers_to_match_api_positions(self, tmp_path):
        """End-to-end: CSV short tickers are remapped so API positions match."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US4581401001", "Ticker": "INTC", "Name": "Intel Corp",
                "No. of shares": "20", "Price / share": "40.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "736.00", "Currency (Total)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"INTC_US_EQ": "STOCK"},
            {"US4581401001": "INTC_US_EQ"},
        )
        mock_client.get_current_positions.return_value = [
            {"ticker": "INTC_US_EQ", "quantity": 20},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        opening = next(s for s in balances if s.referenceDate == date(2024, 1, 1))
        closing = next(s for s in balances if s.referenceDate == date(2025, 1, 1))

        assert opening.quantity == Decimal("0")   # no shares before buy
        assert closing.quantity == Decimal("20")  # bought 20

    def test_hybrid_remaps_dividends_too(self, tmp_path):
        """Dividend tickers are also remapped so they group with the correct security."""
        csv_file = tmp_path / "transactions.csv"
        write_csv(csv_file, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US4581401001", "Ticker": "INTC", "Name": "Intel Corp",
                "No. of shares": "20", "Price / share": "40.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "736.00", "Currency (Total)": "EUR",
            },
            {
                "Action": "Dividend", "Time": "2024-06-15 10:00:00",
                "ISIN": "US4581401001", "Ticker": "INTC", "Name": "Intel Corp",
                "No. of shares": "20", "Price / share": "0.125",
                "Currency (Price / share)": "USD", "Exchange rate": "0.93",
                "Total": "2.33", "Currency (Total)": "EUR",
                "Withholding tax": "0.38", "Currency (Withholding tax)": "EUR",
            },
        ])

        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_instruments_extended.return_value = (
            {"INTC_US_EQ": "STOCK"},
            {"US4581401001": "INTC_US_EQ"},
        )
        mock_client.get_current_positions.return_value = [
            {"ticker": "INTC_US_EQ", "quantity": 20},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_hybrid(str(csv_file))

        # Only one security should exist (orders + dividends grouped under INTC_US_EQ)
        assert len(stmt.listOfSecurities.depot[0].security) == 1
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert len(sec.payment) == 1
        assert sec.payment[0].withHoldingTaxClaim == Decimal("0.38")


# ---------------------------------------------------------------------------
# API-only import (end-to-end with mocked client)
# ---------------------------------------------------------------------------

class TestApiImportEndToEnd:
    """End-to-end test of _import_api() with mocked T212ApiClient."""

    def test_api_import_produces_tax_statement(self, tmp_path):
        """API mode should produce a valid TaxStatement from mocked API data."""
        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_account_summary.return_value = {"id": "123", "currency": "EUR"}
        mock_client.get_instruments.return_value = {"AAPL_US_EQ": "STOCK"}
        mock_client.get_orders.return_value = [
            make_order(side="BUY", ticker="AAPL_US_EQ", filled_at=date(2024, 3, 15)),
        ]
        mock_client.get_dividends.return_value = [
            make_dividend(ticker="AAPL_US_EQ", paid_on=date(2024, 6, 1)),
        ]
        mock_client.get_current_positions.return_value = [
            {"ticker": "AAPL_US_EQ", "quantity": 10},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_api()

        assert stmt.listOfSecurities is not None
        securities = stmt.listOfSecurities.depot[0].security
        assert len(securities) == 1
        sec = securities[0]
        assert sec.symbol == "AAPL_US_EQ"
        assert sec.securityCategory == "SHARE"
        assert len(sec.payment) == 1
        # API dividends have no WHT
        assert sec.payment[0].withHoldingTaxClaim is None
        # Bank accounts should be None (API has no cash history)
        assert stmt.listOfBankAccounts is None

    def test_api_import_instruments_failure_falls_back(self, tmp_path):
        """API mode should still work if get_instruments() fails, defaulting to STOCK."""
        mock_client = MagicMock(spec=T212ApiClient)
        mock_client.get_account_summary.return_value = {"id": "123", "currency": "EUR"}
        mock_client.get_instruments.side_effect = RuntimeError("API down")
        mock_client.get_orders.return_value = [
            make_order(side="BUY", ticker="AAPL_US_EQ", filled_at=date(2024, 3, 15)),
        ]
        mock_client.get_dividends.return_value = []
        mock_client.get_current_positions.return_value = [
            {"ticker": "AAPL_US_EQ", "quantity": 10},
        ]

        settings = make_settings(api_key="test-key")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )

        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=mock_client,
        ):
            stmt = importer._import_api()

        # Should still produce a statement
        assert stmt.listOfSecurities is not None
        assert stmt.listOfSecurities.depot[0].security[0].securityCategory == "SHARE"
        # Should have a critical warning
        from opensteuerauszug.model.critical_warning import CriticalWarningCategory
        other_warnings = [w for w in stmt.critical_warnings if w.category == CriticalWarningCategory.OTHER]
        assert len(other_warnings) >= 1
