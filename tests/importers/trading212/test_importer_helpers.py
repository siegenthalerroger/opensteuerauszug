"""Tests for Trading212 importer helper functions:
type mapping, order/dividend conversion, ISIN validation, country extraction,
model utilities, and importer init edge cases.
"""
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.importers.trading212._models import parse_t212_date
from opensteuerauszug.importers.trading212.trading212_importer import (
    T212_TYPE_TO_ECH,
    Trading212Importer,
    _country_from_isin,
    _dividend_to_payment,
    _extract_instrument_info,
    _orders_to_mutation_stocks,
    _validate_isin,
)

from .conftest import make_dividend, make_order, make_settings


# ---------------------------------------------------------------------------
# Security type mapping
# ---------------------------------------------------------------------------

class TestSecurityTypeMapping:
    def test_stock_maps_to_share(self):
        assert T212_TYPE_TO_ECH["STOCK"] == "SHARE"

    def test_etf_maps_to_fund(self):
        assert T212_TYPE_TO_ECH["ETF"] == "FUND"

    def test_bond_maps_to_bond(self):
        assert T212_TYPE_TO_ECH["BOND"] == "BOND"

    def test_crypto_maps_to_other(self):
        assert T212_TYPE_TO_ECH["CRYPTO"] == "OTHER"

    def test_cryptocurrency_maps_to_other(self):
        assert T212_TYPE_TO_ECH["CRYPTOCURRENCY"] == "OTHER"

    def test_unknown_type_defaults_to_share_in_importer(self):
        result = T212_TYPE_TO_ECH.get("UNKNOWN_TYPE", "SHARE")
        assert result == "SHARE"


# ---------------------------------------------------------------------------
# Order → SecurityStock conversion
# ---------------------------------------------------------------------------

class TestOrderToMutationStocks:
    def test_buy_order_creates_positive_mutation(self):
        order = make_order(side="BUY", quantity="10")
        stocks = _orders_to_mutation_stocks([order], "USD")
        assert len(stocks) == 1
        s = stocks[0]
        assert s.mutation is True
        assert s.quantity == Decimal("10")

    def test_sell_order_creates_negative_mutation(self):
        order = make_order(side="SELL", quantity="5")
        stocks = _orders_to_mutation_stocks([order], "USD")
        assert len(stocks) == 1
        s = stocks[0]
        assert s.mutation is True
        assert s.quantity == Decimal("-5")

    def test_stock_fields_populated_correctly(self):
        order = make_order(side="BUY", filled_at=date(2024, 6, 1), price="100.00", fx_rate="0.90")
        stocks = _orders_to_mutation_stocks([order], "USD")
        s = stocks[0]
        assert s.referenceDate == date(2024, 6, 1)
        assert s.quotationType == "PIECE"
        assert s.unitPrice == Decimal("100.00")
        assert s.exchangeRate == Decimal("0.90")


# ---------------------------------------------------------------------------
# Dividend → SecurityPayment conversion
# ---------------------------------------------------------------------------

class TestDividendToPayment:
    def test_dividend_maps_to_payment(self):
        div = make_dividend(amount="12.50", paid_on=date(2024, 5, 16))
        payment = _dividend_to_payment(div, "EUR")
        assert payment.paymentDate == date(2024, 5, 16)
        assert payment.amount == Decimal("12.50")

    def test_payment_currency_from_instrument(self):
        div = make_dividend(instrument_currency="USD")
        payment = _dividend_to_payment(div, "EUR")
        assert payment.amountCurrency == "USD"

    def test_gross_per_share_mapped_to_amount_per_unit(self):
        div = make_dividend()
        div.gross_per_share = Decimal("0.25")
        payment = _dividend_to_payment(div, "EUR")
        assert payment.amountPerUnit == Decimal("0.25")


# ---------------------------------------------------------------------------
# ISIN validation
# ---------------------------------------------------------------------------

class TestIsinValidation:
    def test_valid_isin_accepted(self):
        result = _validate_isin("US0378331005")
        assert result == "US0378331005"

    def test_invalid_isin_rejected(self):
        assert _validate_isin("INVALID") is None
        assert _validate_isin("us0378331005") is None   # lowercase
        assert _validate_isin("US037833100") is None    # too short

    def test_none_isin_returns_none(self):
        assert _validate_isin(None) is None

    def test_security_without_isin_still_included(self):
        """A security with no valid ISIN should still appear in the output."""
        order = make_order(isin=None)
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([order], [], current_positions=None)
        assert stmt.listOfSecurities is not None
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.isin is None


# ---------------------------------------------------------------------------
# Country extraction from ISIN
# ---------------------------------------------------------------------------

class TestCountryFromIsin:
    def test_us_isin_returns_us(self):
        assert _country_from_isin("US0378331005") == "US"

    def test_de_isin_returns_de(self):
        assert _country_from_isin("DE0005140008") == "DE"

    def test_gb_isin_returns_gb(self):
        assert _country_from_isin("GB0002634946") == "GB"

    def test_xs_eurobond_returns_none(self):
        # XS = international / supra-national, not a country code
        assert _country_from_isin("XS2314659447") is None

    def test_none_returns_none(self):
        assert _country_from_isin(None) is None

    def test_empty_returns_none(self):
        assert _country_from_isin("") is None

    def test_security_gets_country_from_isin(self):
        """Security.country should be derived from the ISIN, not the settings default."""
        order = make_order(isin="US0378331005")
        settings = make_settings(country="GB")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([order], [], current_positions=None)
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.country == "US"

    def test_security_falls_back_to_settings_country_when_no_isin(self):
        """When ISIN is absent, Security.country should use the configured fallback."""
        order = make_order(isin=None)
        settings = make_settings(country="GB")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([order], [], current_positions=None)
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.country == "GB"


# ---------------------------------------------------------------------------
# Instrument type extraction (_extract_instrument_info)
# ---------------------------------------------------------------------------

class TestInstrumentTypeOverride:
    def test_extract_info_defaults_to_stock(self):
        _, _, sec_type, _ = _extract_instrument_info("AAPL_US_EQ", [], [])
        assert sec_type == "STOCK"

    def test_extract_info_uses_override(self):
        _, _, sec_type, _ = _extract_instrument_info("VWCE_EQ", [], [], sec_type_override="ETF")
        assert sec_type == "ETF"

    def test_etf_categorised_as_fund_via_instrument_types(self):
        """instrument_types from /instruments endpoint should route ETF → FUND."""
        order = make_order(ticker="VWCE_EQ", isin="IE00B4L5Y983", name="Vanguard FTSE")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [order], [], current_positions=None,
            instrument_types={"VWCE_EQ": "ETF"},
        )
        assert stmt.listOfSecurities is not None
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.securityCategory == "FUND"

    def test_cryptocurrency_type_skipped_when_ignore_crypto_true(self):
        """CRYPTOCURRENCY type from /instruments endpoint should be filtered out."""
        order = make_order(ticker="BTC_EQ", isin=None, name="Bitcoin")
        settings = make_settings(ignore_crypto=True)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [order], [], current_positions=None,
            instrument_types={"BTC_EQ": "CRYPTOCURRENCY"},
        )
        assert stmt.listOfSecurities is None

    def test_cryptocurrency_type_included_when_ignore_crypto_false(self):
        order = make_order(ticker="BTC_EQ", isin=None, name="Bitcoin")
        settings = make_settings(ignore_crypto=False)
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement(
            [order], [], current_positions=None,
            instrument_types={"BTC_EQ": "CRYPTOCURRENCY"},
        )
        assert stmt.listOfSecurities is not None
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.securityCategory == "OTHER"


# ---------------------------------------------------------------------------
# Multi-ticker statement and dividend-only security
# ---------------------------------------------------------------------------

class TestMultiTickerStatement:
    """Multiple tickers should produce separate Security objects."""

    def test_two_tickers_produce_two_securities(self):
        """Two different tickers should create two separate Security entries."""
        order_a = make_order(ticker="AAPL_US_EQ", isin="US0378331005", name="Apple Inc")
        order_b = make_order(ticker="MSFT_US_EQ", isin="US5949181045", name="Microsoft")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([order_a, order_b], [], current_positions=None)
        assert stmt.listOfSecurities is not None
        securities = stmt.listOfSecurities.depot[0].security
        assert len(securities) == 2
        symbols = {s.symbol for s in securities}
        assert symbols == {"AAPL_US_EQ", "MSFT_US_EQ"}

    def test_orders_and_dividends_grouped_by_ticker(self):
        """Orders and dividends for the same ticker should group into one Security."""
        order = make_order(ticker="AAPL_US_EQ")
        div = make_dividend(ticker="AAPL_US_EQ")
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([order], [div], current_positions=None)
        securities = stmt.listOfSecurities.depot[0].security
        assert len(securities) == 1
        sec = securities[0]
        assert sec.symbol == "AAPL_US_EQ"
        assert len(sec.stock) >= 1  # at least mutations + boundaries
        assert len(sec.payment) == 1

    def test_dividend_only_security(self):
        """A ticker with only dividends (no orders) should still produce a Security."""
        div = make_dividend(ticker="AAPL_US_EQ", paid_on=date(2024, 5, 16))
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [div], current_positions=None)
        # Dividend-only: no stocks/mutations, but payments should appear
        assert stmt.listOfSecurities is not None
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert sec.symbol == "AAPL_US_EQ"
        assert len(sec.payment) == 1
        assert sec.payment[0].amount == Decimal("5.00")


# ---------------------------------------------------------------------------
# Converter fallback tests
# ---------------------------------------------------------------------------

class TestConverterFallbacks:
    """Test fallback behavior when optional fields are None."""

    def test_order_with_none_currency_uses_fallback(self):
        """When order.currency is None, the fallback currency param should be used."""
        order = make_order()
        order.currency = None
        stocks = _orders_to_mutation_stocks([order], "CHF")
        assert stocks[0].balanceCurrency == "CHF"

    def test_order_with_none_price_sets_unit_price_none(self):
        """When order.price is falsy, unitPrice should be None."""
        order = make_order()
        order.price = Decimal("0")
        stocks = _orders_to_mutation_stocks([order], "USD")
        assert stocks[0].unitPrice is None

    def test_dividend_with_none_instrument_currency_uses_fallback(self):
        """When div.instrument_currency is None, the fallback currency should be used."""
        div = make_dividend()
        div.instrument_currency = None
        payment = _dividend_to_payment(div, "CHF")
        assert payment.amountCurrency == "CHF"

    def test_extract_info_resolves_isin_from_dividends(self):
        """When orders have no ISIN, _extract_instrument_info should get it from dividends."""
        order = make_order(isin=None)
        div = make_dividend(isin="US0378331005")
        isin, _, _, _ = _extract_instrument_info("AAPL_US_EQ", [order], [div])
        assert isin == "US0378331005"

    def test_extract_info_with_empty_orders_and_dividends(self):
        """With no orders or dividends, name=ticker, currency='', sec_type='STOCK'."""
        isin, name, sec_type, currency = _extract_instrument_info("XYZ_EQ", [], [])
        assert isin is None
        assert name == "XYZ_EQ"
        assert sec_type == "STOCK"
        assert currency == ""


# ---------------------------------------------------------------------------
# parse_t212_date unit tests
# ---------------------------------------------------------------------------

class TestParseT212Date:
    """Test date parsing utility."""

    def test_iso_datetime_parsed(self):
        """ISO 8601 datetime should parse to a date."""
        result = parse_t212_date("2024-06-01T10:30:00+00:00")
        assert result == date(2024, 6, 1)

    def test_csv_datetime_parsed(self):
        """CSV-style datetime (space separator) should parse to a date."""
        result = parse_t212_date("2024-06-01 10:30:00")
        assert result == date(2024, 6, 1)

    def test_date_only_parsed(self):
        """Plain date string should parse correctly."""
        result = parse_t212_date("2024-06-01")
        assert result == date(2024, 6, 1)

    def test_empty_string_raises(self):
        """Empty string should raise ValueError."""
        with pytest.raises(ValueError, match="Empty date string"):
            parse_t212_date("")

    def test_whitespace_only_raises(self):
        """Whitespace-only string should raise ValueError."""
        with pytest.raises(ValueError, match="Empty date string"):
            parse_t212_date("   ")

    def test_invalid_date_raises(self):
        """Invalid date string should raise ValueError."""
        with pytest.raises(ValueError):
            parse_t212_date("not-a-date")


# ---------------------------------------------------------------------------
# ISIN edge cases
# ---------------------------------------------------------------------------

class TestIsinEdgeCases:
    """Additional ISIN validation and country extraction edge cases."""

    def test_too_long_isin_rejected(self):
        """ISIN with extra characters should be rejected."""
        assert _validate_isin("US0378331005X") is None

    def test_country_from_isin_qz_prefix_returns_none(self):
        """QZ prefix (reserved) should return None."""
        assert _country_from_isin("QZ1234567890") is None

    def test_country_from_isin_lowercase_normalised(self):
        """Lowercase ISIN prefix should be normalised to uppercase."""
        result = _country_from_isin("us0378331005")
        assert result == "US"


# ---------------------------------------------------------------------------
# Importer init edge cases
# ---------------------------------------------------------------------------

class TestImporterInit:
    """Edge cases for Trading212Importer constructor."""

    def test_empty_account_settings_list(self):
        """Empty account_settings_list should set _settings to None."""
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[],
        )
        assert importer._settings is None

    def test_multiple_accounts_uses_first(self):
        """Multiple accounts should use the first one only."""
        settings_a = make_settings(account_number="A")
        settings_b = make_settings(account_number="B")
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings_a, settings_b],
        )
        assert importer._settings.account_number == "A"
