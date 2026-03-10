"""Tests for T212ApiClient: auth, pagination, get_orders, get_instruments, retry."""
import time
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests

from opensteuerauszug.importers.trading212.api_client import RateLimiter, T212ApiClient
from opensteuerauszug.importers.trading212._models import parse_t212_date


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_does_not_sleep_within_limit(self):
        limiter = RateLimiter(max_calls=6, period_seconds=60.0)
        start = time.monotonic()
        for _ in range(6):
            limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "Should not sleep for calls within the limit"

    def test_sleeps_when_limit_exceeded(self):
        """When 6 calls happen instantly, the 7th should trigger a sleep."""
        limiter = RateLimiter(max_calls=3, period_seconds=0.5)
        for _ in range(3):
            limiter.wait()
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.4, f"Expected sleep, got {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestApiClientPagination:
    def test_pagination_iterates_until_null(self):
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)

        page1 = {"items": [{"id": 1}, {"id": 2}], "nextPagePath": "/api/v0/equity/history/orders?cursor=2"}
        page2 = {"items": [{"id": 3}], "nextPagePath": None}

        client._session.get.return_value.json.side_effect = [page1, page2]
        client._session.get.return_value.raise_for_status = MagicMock()

        result = client._paginate("/api/v0/equity/history/orders", client._history_limiter)
        assert len(result) == 3
        assert result[0]["id"] == 1
        assert result[2]["id"] == 3


class TestPaginateStopBefore:
    def _make_client(self):
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        return client

    def _div_date(self, item):
        s = item.get("paidOn", "")
        return parse_t212_date(s) if s else None

    def test_stops_when_oldest_item_before_stop_date(self):
        """Pagination should stop after page 1 because its last item is before stop_before."""
        client = self._make_client()

        page1 = {
            "items": [
                {"paidOn": "2024-06-01T00:00:00"},
                {"paidOn": "2023-11-01T00:00:00"},  # oldest on page — before 2024-01-01
            ],
            "nextPagePath": "/api/v0/equity/history/dividends?cursor=x",
        }
        # Page 2 should never be fetched
        page2 = {"items": [{"paidOn": "2022-01-01T00:00:00"}], "nextPagePath": None}

        client._session.get.return_value.json.side_effect = [page1, page2]
        client._session.get.return_value.raise_for_status = MagicMock()

        result = client._paginate(
            "/api/v0/equity/history/dividends",
            client._history_limiter,
            stop_before=date(2024, 1, 1),
            date_fn=self._div_date,
        )
        # Only items from page1 should be present; page2 was never fetched
        assert len(result) == 2
        assert client._session.get.call_count == 1

    def test_continues_when_oldest_item_at_or_after_stop_date(self):
        """Pagination should continue when all items on a page are at or after stop_before."""
        client = self._make_client()

        page1 = {
            "items": [
                {"paidOn": "2024-06-01T00:00:00"},
                {"paidOn": "2024-01-15T00:00:00"},  # oldest on page — still >= 2024-01-01
            ],
            "nextPagePath": "/api/v0/equity/history/dividends?cursor=x",
        }
        page2 = {"items": [{"paidOn": "2023-12-01T00:00:00"}], "nextPagePath": None}

        client._session.get.return_value.json.side_effect = [page1, page2]
        client._session.get.return_value.raise_for_status = MagicMock()

        result = client._paginate(
            "/api/v0/equity/history/dividends",
            client._history_limiter,
            stop_before=date(2024, 1, 1),
            date_fn=self._div_date,
        )
        assert len(result) == 3
        assert client._session.get.call_count == 2

    def test_no_stop_before_fetches_all_pages(self):
        """Without stop_before, behaviour is unchanged."""
        client = self._make_client()

        page1 = {"items": [{"id": 1}], "nextPagePath": "/api/v0/x?cursor=1"}
        page2 = {"items": [{"id": 2}], "nextPagePath": None}

        client._session.get.return_value.json.side_effect = [page1, page2]
        client._session.get.return_value.raise_for_status = MagicMock()

        result = client._paginate("/api/v0/x", client._history_limiter)
        assert len(result) == 2

    def test_date_fn_exception_continues_pagination(self):
        """If date_fn raises, pagination should continue (not abort)."""
        client = self._make_client()

        def _bad_date_fn(item):
            raise ValueError("unexpected date format")

        page1 = {"items": [{"id": 1}], "nextPagePath": "/api/v0/x?cursor=1"}
        page2 = {"items": [{"id": 2}], "nextPagePath": None}

        client._session.get.return_value.json.side_effect = [page1, page2]
        client._session.get.return_value.raise_for_status = MagicMock()

        # Should not raise, should fetch both pages
        result = client._paginate(
            "/api/v0/x",
            client._history_limiter,
            stop_before=date(2024, 1, 1),
            date_fn=_bad_date_fn,
        )
        assert len(result) == 2
        assert client._session.get.call_count == 2


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestApiClientAuth:
    def test_legacy_auth_sets_authorization_header(self):
        """Single api_key (no secret) should use legacy Authorization header."""
        client = T212ApiClient(api_key="my-key")
        assert client._session.headers.get("Authorization") == "my-key"
        assert client._session.auth is None

    def test_key_pair_auth_uses_basic_auth(self):
        """When api_secret is provided, HTTP Basic auth should be used."""
        client = T212ApiClient(api_key="my-key", api_secret="my-secret")
        assert client._session.auth == ("my-key", "my-secret")
        # Authorization header should NOT be set to the raw key
        assert client._session.headers.get("Authorization") != "my-key"

    def test_accept_header_set(self):
        """Accept: application/json should always be present."""
        client = T212ApiClient(api_key="my-key")
        assert client._session.headers.get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# get_orders: fill quantity sign handling
# ---------------------------------------------------------------------------

class TestApiClientGetOrders:
    """T212 API returns negative fill.quantity for SELL orders.
    get_orders must produce T212Order.quantity as a positive value so that
    _orders_to_mutation_stocks can correctly negate it for SELL.
    """

    def _make_client_with_orders(self, raw_items: list) -> T212ApiClient:
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": raw_items, "nextPagePath": None}
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def _filled_order_item(self, side: str, fill_qty: float) -> dict:
        return {
            "order": {
                "side": side,
                "ticker": "INTC_US_EQ",
                "status": "FILLED",
                "instrument": {"ticker": "INTC_US_EQ", "name": "Intel", "isin": "US4581401001", "currency": "USD"},
                "createdAt": "2025-01-17T10:00:00+00:00",
            },
            "fill": {
                "filledAt": "2025-01-17T10:00:00+00:00",
                "quantity": fill_qty,
                "price": 20.5,
                "walletImpact": {"currency": "CHF", "netValue": 100.0, "fxRate": 0.9},
            },
        }

    def test_sell_negative_fill_qty_becomes_positive_in_order(self):
        """T212 API returns negative fill.quantity for SELL — must be made positive."""
        client = self._make_client_with_orders([self._filled_order_item("SELL", -100.0)])
        orders = client.get_orders()
        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].quantity > 0, "T212Order.quantity must be positive for SELL"
        assert orders[0].quantity == Decimal("100")

    def test_buy_positive_fill_qty_stays_positive(self):
        """T212 API returns positive fill.quantity for BUY — should stay positive."""
        client = self._make_client_with_orders([self._filled_order_item("BUY", 50.0)])
        orders = client.get_orders()
        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].quantity == Decimal("50")

    def test_sell_mutation_stock_has_negative_quantity(self):
        """End-to-end: SELL order from API with negative fill qty → negative SecurityStock."""
        from opensteuerauszug.importers.trading212.trading212_importer import _orders_to_mutation_stocks
        client = self._make_client_with_orders([self._filled_order_item("SELL", -100.0)])
        orders = client.get_orders()
        stocks = _orders_to_mutation_stocks(orders, "CHF")
        assert len(stocks) == 1
        assert stocks[0].quantity == Decimal("-100"), "SELL mutation stock must have negative quantity"


# ---------------------------------------------------------------------------
# get_instruments
# ---------------------------------------------------------------------------

class TestGetInstruments:
    def _make_client(self, response_data):
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._instrument_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response_data
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def test_returns_ticker_to_type_mapping(self):
        client = self._make_client([
            {"ticker": "AAPL_US_EQ", "type": "STOCK", "isin": "US0378331005"},
            {"ticker": "VWCE_EQ", "type": "ETF", "isin": "IE00B3RBWM25"},
            {"ticker": "BTC_EQ", "type": "CRYPTOCURRENCY"},
        ])
        result = client.get_instruments()
        assert result == {
            "AAPL_US_EQ": "STOCK",
            "VWCE_EQ": "ETF",
            "BTC_EQ": "CRYPTOCURRENCY",
        }

    def test_handles_empty_response(self):
        client = self._make_client([])
        result = client.get_instruments()
        assert result == {}

    def test_skips_entries_without_ticker_or_type(self):
        client = self._make_client([
            {"ticker": "AAPL_US_EQ", "type": "STOCK"},
            {"ticker": "", "type": "ETF"},          # missing ticker → skip
            {"ticker": "XYZ", "type": ""},           # missing type → skip
        ])
        result = client.get_instruments()
        assert result == {"AAPL_US_EQ": "STOCK"}


# ---------------------------------------------------------------------------
# 429 retry with exponential backoff
# ---------------------------------------------------------------------------

class TestRetryOn429:
    def _make_response(self, status_code, json_data=None, retry_after=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers.get = lambda key, default=None: retry_after if key == "Retry-After" else default
        if json_data is not None:
            resp.json.return_value = json_data
        if status_code >= 400:
            resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        else:
            resp.raise_for_status.return_value = None
        return resp

    def _make_client(self, max_retries=3):
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = max_retries
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        return client

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_retries_on_429_and_succeeds(self, mock_sleep):
        """A 429 followed by a 200 should succeed after one retry."""
        client = self._make_client(max_retries=2)
        client._session.get.side_effect = [
            self._make_response(429, retry_after="10"),
            self._make_response(200, json_data={"items": []}),
        ]

        result = client._get("/api/v0/equity/history/orders")

        assert result == {"items": []}
        assert client._session.get.call_count == 2
        mock_sleep.assert_called_once_with(10.0)

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_exponential_backoff_without_retry_after(self, mock_sleep):
        """Without Retry-After header, sleep is 2^attempt + jitter (attempt=0 → [1, 2))."""
        client = self._make_client(max_retries=2)
        client._session.get.side_effect = [
            self._make_response(429),  # no Retry-After
            self._make_response(200, json_data={}),
        ]

        client._get("/api/v0/equity/history/orders")

        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        assert 1.0 <= sleep_arg < 2.0  # 2^0 + uniform(0,1)

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_raises_after_max_retries_exhausted(self, mock_sleep):
        """After max_retries consecutive 429s the final HTTPError propagates."""
        client = self._make_client(max_retries=2)
        client._session.get.return_value = self._make_response(429)

        with pytest.raises(requests.HTTPError):
            client._get("/api/v0/equity/history/orders")

        assert mock_sleep.call_count == 2       # slept after attempt 0 and 1
        assert client._session.get.call_count == 3  # attempt 0, 1, 2

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_no_retry_on_other_http_errors(self, mock_sleep):
        """Non-429 errors (e.g. 403) raise immediately without retrying."""
        client = self._make_client(max_retries=3)
        client._session.get.return_value = self._make_response(403)

        with pytest.raises(requests.HTTPError):
            client._get("/api/v0/equity/history/orders")

        mock_sleep.assert_not_called()
        assert client._session.get.call_count == 1

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_max_retries_zero_raises_immediately_on_429(self, mock_sleep):
        """max_retries=0 means no retries — a 429 raises on the first attempt."""
        client = self._make_client(max_retries=0)
        client._session.get.return_value = self._make_response(429)

        with pytest.raises(requests.HTTPError):
            client._get("/api/v0/equity/history/orders")

        mock_sleep.assert_not_called()
        assert client._session.get.call_count == 1

    @patch("opensteuerauszug.importers.trading212.api_client.time.sleep")
    def test_multiple_429s_then_success(self, mock_sleep):
        """Multiple consecutive 429s followed by a 200 all use their Retry-After values."""
        client = self._make_client(max_retries=3)
        client._session.get.side_effect = [
            self._make_response(429, retry_after="1"),
            self._make_response(429, retry_after="2"),
            self._make_response(200, json_data={"ok": True}),
        ]

        result = client._get("/api/v0/equity/history/orders")

        assert result == {"ok": True}
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0


# ---------------------------------------------------------------------------
# get_orders: skip conditions
# ---------------------------------------------------------------------------

class TestGetOrdersSkipConditions:
    """Orders that don't meet the filter criteria should be silently skipped."""

    def _make_client_with_items(self, items: list) -> T212ApiClient:
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": items, "nextPagePath": None}
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def _valid_order_item(self, **overrides) -> dict:
        """Minimal valid order item with override capability."""
        item = {
            "order": {
                "side": "BUY",
                "ticker": "AAPL_US_EQ",
                "status": "FILLED",
                "instrument": {
                    "ticker": "AAPL_US_EQ",
                    "name": "Apple",
                    "isin": "US0378331005",
                    "currency": "USD",
                },
                "createdAt": "2024-06-01T10:00:00+00:00",
            },
            "fill": {
                "filledAt": "2024-06-01T10:00:00+00:00",
                "quantity": 10.0,
                "price": 150.0,
                "walletImpact": {"currency": "EUR", "netValue": 1380.0, "fxRate": 0.92},
            },
        }
        # Apply overrides to nested dicts
        for key, value in overrides.items():
            if "." in key:
                parts = key.split(".")
                d = item
                for p in parts[:-1]:
                    d = d[p]
                d[parts[-1]] = value
            else:
                item[key] = value
        return item

    def test_skips_order_without_fill_data(self):
        """Orders missing the fill dict should be skipped."""
        item = self._valid_order_item()
        item["fill"] = {}
        client = self._make_client_with_items([item])
        assert client.get_orders() == []

    def test_skips_order_with_non_filled_status(self):
        """Orders with status other than FILLED/PARTIALLY_FILLED should be skipped."""
        item = self._valid_order_item()
        item["order"]["status"] = "CANCELLED"
        client = self._make_client_with_items([item])
        assert client.get_orders() == []

    def test_accepts_partially_filled_status(self):
        """PARTIALLY_FILLED status should be accepted."""
        item = self._valid_order_item()
        item["order"]["status"] = "PARTIALLY_FILLED"
        client = self._make_client_with_items([item])
        orders = client.get_orders()
        assert len(orders) == 1
        assert orders[0].side == "BUY"

    def test_skips_order_with_unrecognized_side(self):
        """Orders with side other than BUY/SELL should be skipped."""
        item = self._valid_order_item()
        item["order"]["side"] = "SHORT"
        client = self._make_client_with_items([item])
        assert client.get_orders() == []

    def test_skips_order_without_ticker(self):
        """Orders with no ticker (neither top-level nor instrument) should be skipped."""
        item = self._valid_order_item()
        item["order"]["ticker"] = ""
        item["order"]["instrument"]["ticker"] = ""
        client = self._make_client_with_items([item])
        assert client.get_orders() == []

    def test_malformed_order_caught_by_except(self):
        """Malformed fill data (e.g. non-numeric quantity) should be caught, not crash."""
        item = self._valid_order_item()
        item["fill"]["quantity"] = "not-a-number"
        client = self._make_client_with_items([item])
        assert client.get_orders() == []

    def test_fx_rate_none_when_missing(self):
        """Missing fxRate in walletImpact should result in fx_rate=None."""
        item = self._valid_order_item()
        item["fill"]["walletImpact"] = {"currency": "EUR", "netValue": 100.0}
        client = self._make_client_with_items([item])
        orders = client.get_orders()
        assert len(orders) == 1
        assert orders[0].fx_rate is None

    def test_net_value_none_defaults_to_zero(self):
        """Missing netValue in walletImpact should default to Decimal('0')."""
        item = self._valid_order_item()
        item["fill"]["walletImpact"] = {"currency": "EUR", "fxRate": 0.92}
        client = self._make_client_with_items([item])
        orders = client.get_orders()
        assert len(orders) == 1
        assert orders[0].total_account_currency == Decimal("0")


# ---------------------------------------------------------------------------
# get_dividends: skip conditions and edge cases
# ---------------------------------------------------------------------------

class TestGetDividendsSkipConditions:
    """Dividends that don't meet filter criteria should be silently skipped."""

    def _make_client_with_items(self, items: list) -> T212ApiClient:
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._history_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": items, "nextPagePath": None}
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def test_skips_dividend_without_ticker(self):
        """Dividends with no ticker should be skipped."""
        item = {
            "ticker": "",
            "instrument": {"ticker": "", "name": "Unknown"},
            "paidOn": "2024-06-01T00:00:00",
            "amount": 5.0,
        }
        client = self._make_client_with_items([item])
        assert client.get_dividends() == []

    def test_malformed_dividend_caught_by_except(self):
        """Malformed dividend data (e.g. bad date) should be caught, not crash."""
        item = {
            "ticker": "AAPL_US_EQ",
            "instrument": {"ticker": "AAPL_US_EQ", "name": "Apple", "isin": "US0378331005"},
            "paidOn": "",  # empty date triggers ValueError in parse_t212_date
            "amount": 5.0,
        }
        client = self._make_client_with_items([item])
        assert client.get_dividends() == []

    def test_dividend_wht_and_fx_are_none(self):
        """API dividends should always have withholding_tax=None and fx_rate=None."""
        item = {
            "ticker": "AAPL_US_EQ",
            "instrument": {"ticker": "AAPL_US_EQ", "name": "Apple", "isin": "US0378331005"},
            "paidOn": "2024-06-01T00:00:00",
            "amount": 5.0,
            "grossAmountPerShare": 0.25,
            "tickerCurrency": "USD",
        }
        client = self._make_client_with_items([item])
        divs = client.get_dividends()
        assert len(divs) == 1
        assert divs[0].withholding_tax is None
        assert divs[0].fx_rate is None

    def test_gross_per_share_none_when_missing(self):
        """Missing grossAmountPerShare should result in gross_per_share=None."""
        item = {
            "ticker": "AAPL_US_EQ",
            "instrument": {"ticker": "AAPL_US_EQ", "name": "Apple"},
            "paidOn": "2024-06-01T00:00:00",
            "amount": 5.0,
        }
        client = self._make_client_with_items([item])
        divs = client.get_dividends()
        assert len(divs) == 1
        assert divs[0].gross_per_share is None


# ---------------------------------------------------------------------------
# get_current_positions: response format handling
# ---------------------------------------------------------------------------

class TestGetCurrentPositions:
    """get_current_positions should handle both list and dict response formats."""

    def _make_client_with_response(self, data) -> T212ApiClient:
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._slow_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def test_list_response_returned_directly(self):
        """When API returns a list, it should be returned as-is."""
        positions = [{"ticker": "AAPL_US_EQ", "quantity": 10}]
        client = self._make_client_with_response(positions)
        result = client.get_current_positions()
        assert result == positions

    def test_dict_response_extracts_items(self):
        """When API returns a dict, items key should be extracted."""
        positions = [{"ticker": "AAPL_US_EQ", "quantity": 10}]
        client = self._make_client_with_response({"items": positions, "meta": {}})
        result = client.get_current_positions()
        assert result == positions

    def test_dict_response_empty_items(self):
        """When API returns a dict without items key, empty list returned."""
        client = self._make_client_with_response({"meta": "data"})
        result = client.get_current_positions()
        assert result == []


# ---------------------------------------------------------------------------
# get_instruments_extended: ISIN-to-ticker mapping
# ---------------------------------------------------------------------------

class TestGetInstrumentsExtended:
    """get_instruments_extended returns both ticker→type and isin→ticker mappings."""

    def _make_client(self, response_data):
        client = T212ApiClient.__new__(T212ApiClient)
        client._session = MagicMock()
        client._max_retries = 0
        client._instrument_limiter = RateLimiter(max_calls=100, period_seconds=1.0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response_data
        resp.raise_for_status = MagicMock()
        client._session.get.return_value = resp
        return client

    def test_isin_to_ticker_mapping_returned(self):
        """The second return value should map ISIN → API ticker."""
        client = self._make_client([
            {"ticker": "AAPL_US_EQ", "type": "STOCK", "isin": "US0378331005"},
            {"ticker": "VWCE_EQ", "type": "ETF", "isin": "IE00B3RBWM25"},
        ])
        ticker_to_type, isin_to_ticker = client.get_instruments_extended()
        assert isin_to_ticker == {
            "US0378331005": "AAPL_US_EQ",
            "IE00B3RBWM25": "VWCE_EQ",
        }
        assert ticker_to_type == {"AAPL_US_EQ": "STOCK", "VWCE_EQ": "ETF"}

    def test_entries_without_isin_excluded_from_isin_map(self):
        """Instruments without ISIN should not appear in isin→ticker mapping."""
        client = self._make_client([
            {"ticker": "BTC_EQ", "type": "CRYPTOCURRENCY"},  # no ISIN
        ])
        ticker_to_type, isin_to_ticker = client.get_instruments_extended()
        assert ticker_to_type == {"BTC_EQ": "CRYPTOCURRENCY"}
        assert isin_to_ticker == {}

    def test_dict_response_format_handled(self):
        """get_instruments_extended should handle dict response with 'items' key."""
        client = self._make_client({
            "items": [{"ticker": "AAPL_US_EQ", "type": "STOCK", "isin": "US0378331005"}]
        })
        ticker_to_type, isin_to_ticker = client.get_instruments_extended()
        assert ticker_to_type == {"AAPL_US_EQ": "STOCK"}
        assert isin_to_ticker == {"US0378331005": "AAPL_US_EQ"}
