"""Tests for T212CsvParser: orders, dividends, cash transactions, stock splits, WHT."""
import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.importers.trading212.csv_parser import T212CsvParser

from .conftest import write_csv


# ---------------------------------------------------------------------------
# Orders and dividends
# ---------------------------------------------------------------------------

class TestCsvParser:
    def _make_csv(self, rows: list[dict], headers: list[str] = None) -> str:
        default_headers = [
            "Action", "Time", "ISIN", "Ticker", "Name",
            "No. of shares", "Price / share", "Currency (Price / share)",
            "Exchange rate", "Total", "Currency (Total)",
        ]
        headers = headers or default_headers
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    def test_parser_reads_buy_order(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Market buy", "Time": "2024-03-15 10:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "10", "Price / share": "150.00",
            "Currency (Price / share)": "USD", "Exchange rate": "0.92",
            "Total": "1380.00", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == Decimal("10")
        assert len(dividends) == 0

    def test_parser_reads_sell_order(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Market sell", "Time": "2024-06-01 14:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "5", "Price / share": "180.00",
            "Currency (Price / share)": "USD", "Exchange rate": "0.91",
            "Total": "819.00", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].side == "SELL"

    def test_parser_reads_dividend(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Dividend", "Time": "2024-05-16 00:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "10", "Price / share": "0.25",
            "Currency (Price / share)": "USD", "Exchange rate": "0.92",
            "Total": "2.30", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 1
        assert dividends[0].amount_account_currency == Decimal("2.30")
        assert dividends[0].gross_per_share == Decimal("0.25")

    def test_parser_gross_per_share_none_when_price_missing(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Dividend", "Time": "2024-05-16 00:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "10", "Price / share": "",
            "Currency (Price / share)": "USD", "Exchange rate": "0.92",
            "Total": "2.30", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        _, dividends, _ = parser.parse()
        assert dividends[0].gross_per_share is None

    @pytest.mark.parametrize("action", [
        "Dividend (Ordinary)",
        "Dividend (Dividends paid by us corporations)",
        "Dividend (Dividends paid by foreign corporations)",
        "Dividend (Bonus)",
        "Dividend (Return of capital non us)",
        "Dividend (Dividend)",
    ])
    def test_parser_reads_dividend_subtypes(self, tmp_path, action):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": action, "Time": "2024-05-16 00:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "10", "Price / share": "0.25",
            "Currency (Price / share)": "USD", "Exchange rate": "0.92",
            "Total": "2.30", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 1
        assert dividends[0].amount_account_currency == Decimal("2.30")

    def test_parser_reads_stop_limit_sell(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Stop limit sell", "Time": "2024-06-01 14:00:00",
            "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
            "No. of shares": "5", "Price / share": "180.00",
            "Currency (Price / share)": "USD", "Exchange rate": "0.91",
            "Total": "819.00", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity == Decimal("5")

    def test_parser_ignores_result_adjustment(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Result adjustment", "Time": "2024-01-04 08:46:14",
            "Total": "0.01", "Currency (Total)": "CHF",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, cash_txs = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 0
        assert len(cash_txs) == 0

    def test_parser_ignores_deposits(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(self._make_csv([{
            "Action": "Deposit", "Time": "2024-01-05 00:00:00",
            "Total": "1000.00", "Currency (Total)": "EUR",
        }]))
        parser = T212CsvParser(str(csv_path))
        orders, dividends, _ = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 0

    def test_parser_missing_required_column_raises(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        # Missing 'Action' column
        csv_path.write_text("Time,ISIN,Ticker\n2024-01-01,US123,AAPL\n")
        parser = T212CsvParser(str(csv_path))
        with pytest.raises(ValueError, match="missing required columns"):
            parser.parse()


# ---------------------------------------------------------------------------
# Withholding tax parsing
# ---------------------------------------------------------------------------

class TestCsvWithholdingTax:
    def test_withholding_tax_parsed_from_csv(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        headers = [
            "Action", "Time", "ISIN", "Ticker", "Name",
            "No. of shares", "Price / share", "Currency (Price / share)",
            "Exchange rate", "Total", "Currency (Total)",
            "Withholding tax", "Currency (Withholding tax)",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerow({
                "Action": "Dividend", "Time": "2024-05-16 00:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
                "No. of shares": "10", "Price / share": "0.25",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "2.30", "Currency (Total)": "EUR",
                "Withholding tax": "0.35", "Currency (Withholding tax)": "USD",
            })
        parser = T212CsvParser(str(csv_path))
        _, dividends, _ = parser.parse()
        assert len(dividends) == 1
        assert dividends[0].withholding_tax == Decimal("0.35")

    def test_missing_withholding_tax_is_none(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        headers = [
            "Action", "Time", "ISIN", "Ticker", "Name",
            "No. of shares", "Price / share", "Currency (Price / share)",
            "Exchange rate", "Total", "Currency (Total)",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerow({
                "Action": "Dividend", "Time": "2024-05-16 00:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
                "No. of shares": "10", "Price / share": "0.25",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "2.30", "Currency (Total)": "EUR",
            })
        parser = T212CsvParser(str(csv_path))
        _, dividends, _ = parser.parse()
        assert dividends[0].withholding_tax is None


# ---------------------------------------------------------------------------
# Stock split handling
# ---------------------------------------------------------------------------

class TestStockSplitParsing:
    def test_split_close_parsed_as_sell(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Stock split close",
                "Time": "2025-02-18 07:38:10",
                "ISIN": "US67421J2078",
                "Ticker": "OTLY",
                "Name": "Oatly",
                "No. of shares": "15000",
                "Price / share": "1.37",
                "Currency (Price / share)": "USD",
                "Exchange rate": "1.10",
                "Total": "18620.49",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity == Decimal("15000")
        assert orders[0].price == Decimal("0")  # Split, no meaningful price

    def test_split_open_parsed_as_buy(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Stock split open",
                "Time": "2025-02-18 07:38:10",
                "ISIN": "US67421J2078",
                "Ticker": "OTLY",
                "Name": "Oatly",
                "No. of shares": "750",
                "Price / share": "27.42",
                "Currency (Price / share)": "USD",
                "Exchange rate": "1.10",
                "Total": "18620.49",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == Decimal("750")
        assert orders[0].price == Decimal("0")

    def test_reverse_split_adjusts_position(self):
        """A reverse split (close 15000, open 750) should net to -14250 shares."""
        from datetime import date
        from opensteuerauszug.importers.trading212._models import T212Order
        from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer

        buy = T212Order(
            filled_at=date(2025, 1, 15), side="BUY", ticker="OTLY", name="Oatly",
            isin="US67421J2078", quantity=Decimal("15000"), price=Decimal("150.00"),
            currency="USD", fx_rate=None, total_account_currency=Decimal("0"),
        )
        split_close = T212Order(
            filled_at=date(2025, 2, 18), side="SELL", ticker="OTLY", name="Oatly",
            isin="US67421J2078", quantity=Decimal("15000"), price=Decimal("0"),
            currency="USD", fx_rate=None, total_account_currency=Decimal("0"),
        )
        split_open = T212Order(
            filled_at=date(2025, 2, 18), side="BUY", ticker="OTLY", name="Oatly",
            isin="US67421J2078", quantity=Decimal("750"), price=Decimal("0"),
            currency="USD", fx_rate=None, total_account_currency=Decimal("0"),
        )

        from .conftest import make_settings
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2025, 1, 1),
            period_to=date(2025, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [buy, split_close, split_open], [], current_positions=None,
        )

        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = [s for s in sec.stock if not s.mutation]
        closing = next(s for s in balances if s.referenceDate == date(2026, 1, 1))
        # 0 (start) + 15000 (buy) - 15000 (split close) + 750 (split open) = 750
        assert closing.quantity == Decimal("750")


# ---------------------------------------------------------------------------
# Cash transactions (interest / lending income)
# ---------------------------------------------------------------------------

class TestCashTransactionParsing:
    """CSV parser extracts interest and lending income rows."""

    def test_interest_on_cash_parsed(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Interest on cash",
                "Time": "2024-06-15 00:00:00",
                "Total": "0.16",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        _, _, cash_txs = parser.parse()
        assert len(cash_txs) == 1
        assert cash_txs[0].action == "Interest on cash"
        assert cash_txs[0].amount == Decimal("0.16")
        assert cash_txs[0].currency == "CHF"

    def test_lending_interest_parsed(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Lending interest",
                "Time": "2024-06-15 00:00:00",
                "Total": "3.55",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        _, _, cash_txs = parser.parse()
        assert len(cash_txs) == 1
        assert cash_txs[0].action == "Lending interest"
        assert cash_txs[0].amount == Decimal("3.55")

    def test_interest_no_longer_ignored(self, tmp_path):
        """Interest on cash was previously in IGNORED_ACTIONS; now it's parsed."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Interest on cash",
                "Time": "2024-06-15 00:00:00",
                "Total": "1.00",
                "Currency (Total)": "CHF",
            },
            {
                "Action": "Deposit",
                "Time": "2024-01-01 00:00:00",
                "Total": "1000.00",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, dividends, cash_txs = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 0
        assert len(cash_txs) == 1  # Interest parsed, Deposit still ignored


# ---------------------------------------------------------------------------
# Edge cases: empty file, unknown action, malformed rows
# ---------------------------------------------------------------------------

class TestCsvEdgeCases:
    """CSV parser edge cases: empty files, unknown actions, malformed data."""

    def test_empty_csv_file_raises(self, tmp_path):
        """A completely empty CSV file should raise ValueError."""
        csv_path = tmp_path / "empty.csv"
        csv_path.write_text("")
        parser = T212CsvParser(str(csv_path))
        with pytest.raises(ValueError, match="empty"):
            parser.parse()

    def test_unknown_action_silently_skipped(self, tmp_path):
        """Rows with unrecognized actions should be skipped without error."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Some Future Action Type",
                "Time": "2024-06-01 10:00:00",
                "Ticker": "AAPL_US_EQ",
                "No. of shares": "10",
                "Price / share": "150.00",
                "Currency (Price / share)": "USD",
                "Total": "1380.00",
                "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, dividends, cash_txs = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 0
        assert len(cash_txs) == 0

    def test_order_with_missing_ticker_skipped(self, tmp_path):
        """An order row with empty ticker should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "", "Name": "Apple",
                "No. of shares": "10", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 0

    def test_order_with_missing_quantity_skipped(self, tmp_path):
        """An order row with empty quantity should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
                "No. of shares": "", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 0

    def test_order_with_missing_price_skipped(self, tmp_path):
        """An order row with empty price should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
                "No. of shares": "10", "Price / share": "",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "1380.00", "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 0

    def test_dividend_with_missing_ticker_skipped(self, tmp_path):
        """A dividend row with empty ticker should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Dividend", "Time": "2024-05-16 00:00:00",
                "ISIN": "US0378331005", "Ticker": "", "Name": "Apple",
                "No. of shares": "10", "Price / share": "0.25",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "2.30", "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        _, dividends, _ = parser.parse()
        assert len(dividends) == 0

    def test_split_with_missing_quantity_skipped(self, tmp_path):
        """A stock split row with empty quantity should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Stock split close", "Time": "2025-02-18 07:38:10",
                "ISIN": "US67421J2078", "Ticker": "OTLY", "Name": "Oatly",
                "No. of shares": "",
                "Price / share": "1.37", "Currency (Price / share)": "USD",
                "Exchange rate": "1.10", "Total": "0", "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 0

    def test_comma_formatted_numbers_parsed(self, tmp_path):
        """Numbers with commas (e.g. '1,380.00') should be parsed correctly."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Market buy", "Time": "2024-03-15 10:00:00",
                "ISIN": "US0378331005", "Ticker": "AAPL_US_EQ", "Name": "Apple",
                "No. of shares": "1,000", "Price / share": "150.00",
                "Currency (Price / share)": "USD", "Exchange rate": "0.92",
                "Total": "138,000.00", "Currency (Total)": "EUR",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].quantity == Decimal("1000")
        assert orders[0].total_account_currency == Decimal("138000.00")

    def test_empty_action_rows_skipped(self, tmp_path):
        """Rows with empty action field should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "", "Time": "2024-03-15 10:00:00",
                "Ticker": "AAPL_US_EQ",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, dividends, cash_txs = parser.parse()
        assert len(orders) == 0
        assert len(dividends) == 0
        assert len(cash_txs) == 0

    def test_utf8_bom_handled(self, tmp_path):
        """CSV files with a UTF-8 BOM (utf-8-sig) should parse correctly."""
        csv_path = tmp_path / "t.csv"
        # Write with utf-8-sig encoding (which prepends the BOM automatically)
        content = (
            "Action,Time,ISIN,Ticker,Name,No. of shares,"
            "Price / share,Currency (Price / share),Exchange rate,"
            "Total,Currency (Total)\r\n"
            "Market buy,2024-03-15 10:00:00,US0378331005,AAPL_US_EQ,Apple,"
            "10,150.00,USD,0.92,1380.00,EUR\r\n"
        )
        csv_path.write_text(content, encoding="utf-8-sig")
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 1
        assert orders[0].ticker == "AAPL_US_EQ"

    def test_split_with_missing_ticker_skipped(self, tmp_path):
        """A stock split row with empty ticker should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Stock split open", "Time": "2025-02-18 07:38:10",
                "ISIN": "US67421J2078", "Ticker": "", "Name": "Oatly",
                "No. of shares": "750",
                "Price / share": "27.42", "Currency (Price / share)": "USD",
                "Exchange rate": "1.10", "Total": "0", "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        orders, _, _ = parser.parse()
        assert len(orders) == 0

    def test_cash_transaction_with_missing_amount_skipped(self, tmp_path):
        """An interest row with empty amount should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Interest on cash", "Time": "2024-06-15 00:00:00",
                "Total": "",
                "Currency (Total)": "CHF",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        _, _, cash_txs = parser.parse()
        assert len(cash_txs) == 0

    def test_cash_transaction_with_missing_currency_skipped(self, tmp_path):
        """An interest row with empty currency should be skipped."""
        csv_path = tmp_path / "t.csv"
        write_csv(csv_path, [
            {
                "Action": "Interest on cash", "Time": "2024-06-15 00:00:00",
                "Total": "1.50",
                "Currency (Total)": "",
            },
        ])
        parser = T212CsvParser(str(csv_path))
        _, _, cash_txs = parser.parse()
        assert len(cash_txs) == 0
