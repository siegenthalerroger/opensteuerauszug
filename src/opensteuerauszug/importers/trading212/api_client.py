"""Trading212 REST API client."""
import logging
import random
import time
from collections import deque
from datetime import date
from decimal import Decimal
from typing import Callable, Dict, Optional
from urllib.parse import parse_qsl, urlparse

import requests

from ._models import T212Dividend, T212Order, parse_t212_date

logger = logging.getLogger(__name__)

BASE_URL = "https://live.trading212.com"


class RateLimiter:
    """Enforces a sliding-window rate limit."""

    def __init__(self, max_calls: int, period_seconds: float):
        self._max_calls = max_calls
        self._period = period_seconds
        self._call_times: deque = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._call_times and now - self._call_times[0] > self._period:
            self._call_times.popleft()
        if len(self._call_times) >= self._max_calls:
            oldest = self._call_times[0]
            sleep_for = self._period - (now - oldest) + 0.1
            if sleep_for > 0:
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._call_times and now - self._call_times[0] > self._period:
                self._call_times.popleft()
        self._call_times.append(time.monotonic())


class T212ApiClient:
    """
    Client for the Trading212 REST API.

    All history endpoints return items in reverse-chronological order and
    have no server-side date filter, so we fetch everything and filter
    client-side.
    """

    def __init__(self, api_key: str, api_secret: Optional[str] = None, max_retries: int = 3):
        self._session = requests.Session()
        # T212 supports two auth schemes (from OpenAPI spec):
        #   - New key-pair: HTTP Basic auth with api_key:api_secret
        #   - Legacy single-key: raw api_key in the Authorization header
        if api_secret:
            self._session.auth = (api_key, api_secret)
        else:
            self._session.headers.update({"Authorization": api_key})
        self._session.headers.update({"Accept": "application/json"})
        self._max_retries = max_retries
        # History endpoints: 6 req / 60 s
        self._history_limiter = RateLimiter(max_calls=6, period_seconds=60.0)
        # Slower endpoints: 1 req / 5 s
        self._slow_limiter = RateLimiter(max_calls=1, period_seconds=5.0)
        # Instruments metadata endpoint: 1 req / 50 s
        self._instrument_limiter = RateLimiter(max_calls=1, period_seconds=50.0)

    def _get(self, path: str, params: Optional[dict] = None, limiter: Optional[RateLimiter] = None) -> dict:
        if limiter:
            limiter.wait()
        url = BASE_URL + path
        for attempt in range(self._max_retries + 1):
            resp = self._session.get(url, params=params)
            if resp.status_code == 429 and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    sleep_for = float(retry_after)
                except (TypeError, ValueError):
                    sleep_for = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "429 Too Many Requests for %s; retrying in %.1fs (attempt %d/%d)",
                    path, sleep_for, attempt + 1, self._max_retries,
                )
                time.sleep(sleep_for)
                continue
            resp.raise_for_status()
            return resp.json()
        # Unreachable: the final loop iteration always raise_for_status or returns.
        resp.raise_for_status()  # type: ignore[reportPossiblyUnbound]
        return resp.json()  # type: ignore[reportPossiblyUnbound]

    def _paginate(
        self,
        path: str,
        limiter: RateLimiter,
        stop_before: Optional[date] = None,
        date_fn: Optional[Callable[[dict], Optional[date]]] = None,
    ) -> list:
        """Fetch all pages of a paginated endpoint.

        ``stop_before`` / ``date_fn``: when both are supplied and the oldest
        item on a page has a date before ``stop_before``, pagination stops
        early.  The API returns items in reverse-chronological order so the
        last item on a page is always the oldest.  Items already fetched are
        kept; downstream callers filter by date as usual.
        """
        results = []
        params: dict = {"limit": 50}
        while True:
            data = self._get(path, params=params, limiter=limiter)
            items = data.get("items", [])
            results.extend(items)

            # Early termination: the oldest item on this page is at items[-1].
            if stop_before and date_fn and items:
                try:
                    oldest = date_fn(items[-1])
                    if oldest is not None and oldest < stop_before:
                        logger.debug(
                            "_paginate: oldest item (%s) before stop_before=%s, stopping.",
                            oldest, stop_before,
                        )
                        break
                except Exception as exc:
                    logger.debug("_paginate: date extraction failed, continuing: %s", exc)

            next_path = data.get("nextPagePath")
            if not next_path or not items:
                break
            # nextPagePath is a relative path with cursor embedded
            parsed = urlparse(next_path)
            params = dict(parse_qsl(parsed.query))
            path = parsed.path
        return results

    def get_account_summary(self) -> dict:
        """Return account summary (id, currency, etc.)."""
        logger.info("Fetching T212 account summary")
        return self._get("/api/v0/equity/account/summary", limiter=self._slow_limiter)

    def get_current_positions(self) -> list[dict]:
        """Return all currently open positions."""
        logger.info("Fetching T212 current positions")
        data = self._get("/api/v0/equity/positions", limiter=self._slow_limiter)
        if isinstance(data, list):
            return data
        return data.get("items", [])

    def get_instruments(self) -> Dict[str, str]:
        """Fetch all accessible instruments and return a ticker → type mapping.

        The ``type`` field values match those in ``T212_TYPE_TO_ECH``
        (e.g. ``"STOCK"``, ``"ETF"``, ``"CRYPTOCURRENCY"``).

        Rate limit: 1 req / 50 s.
        """
        ticker_to_type, _ = self.get_instruments_extended()
        return ticker_to_type

    def get_instruments_extended(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Fetch instruments and return (ticker→type, isin→api_ticker) mappings.

        The ISIN→ticker mapping allows bridging between CSV short tickers
        (e.g. ``INTC``) and API qualified tickers (e.g. ``INTC_US_EQ``)
        using the ISIN as a common key.

        Rate limit: 1 req / 50 s.
        """
        logger.info("Fetching T212 instrument metadata...")
        data = self._get(
            "/api/v0/equity/metadata/instruments", limiter=self._instrument_limiter
        )
        instruments = data if isinstance(data, list) else data.get("items", [])
        ticker_to_type: Dict[str, str] = {}
        isin_to_ticker: Dict[str, str] = {}
        for instr in instruments:
            ticker = instr.get("ticker", "")
            itype = instr.get("type", "")
            isin = instr.get("isin", "")
            if ticker and itype:
                ticker_to_type[ticker] = itype
            if isin and ticker:
                isin_to_ticker[isin] = ticker
        logger.info(
            "Retrieved %d instrument type entries and %d ISIN mappings from T212 API",
            len(ticker_to_type), len(isin_to_ticker),
        )
        return ticker_to_type, isin_to_ticker

    def get_orders(self, stop_before: Optional[date] = None) -> list[T212Order]:
        """Fetch all historical filled orders and return as T212Order objects.

        ``stop_before``: stop paginating when the oldest item on a page has a
        date before this date (API mode optimisation — older mutations are not
        needed when a live position anchor is available).
        """
        logger.info("Fetching T212 order history (all pages)...")

        def _order_date(item: dict) -> Optional[date]:
            fill = item.get("fill") or {}
            order = item.get("order") or {}
            date_str = fill.get("filledAt") or order.get("createdAt", "")
            return parse_t212_date(date_str) if date_str else None

        raw_orders = self._paginate(
            "/api/v0/equity/history/orders",
            self._history_limiter,
            stop_before=stop_before,
            date_fn=_order_date,
        )
        result = []
        for item in raw_orders:
            order = item.get("order", {})
            fill = item.get("fill", {})
            if not fill:
                continue
            status = order.get("status", "")
            if status not in ("FILLED", "PARTIALLY_FILLED"):
                continue
            side = order.get("side", "")
            if side not in ("BUY", "SELL"):
                continue
            instrument = order.get("instrument") or {}
            ticker = order.get("ticker") or instrument.get("ticker", "")
            if not ticker:
                continue
            try:
                filled_at_str = fill.get("filledAt") or order.get("createdAt", "")
                filled_at = parse_t212_date(filled_at_str)
                qty = abs(Decimal(str(fill.get("quantity") or order.get("filledQuantity", 0))))
                price = Decimal(str(fill.get("price", 0)))
                wallet = fill.get("walletImpact") or {}
                fx_rate_raw = wallet.get("fxRate")
                fx_rate = Decimal(str(fx_rate_raw)) if fx_rate_raw is not None else None
                net_value_raw = wallet.get("netValue")
                net_value = Decimal(str(net_value_raw)) if net_value_raw is not None else Decimal("0")
                result.append(T212Order(
                    filled_at=filled_at,
                    side=side,
                    ticker=ticker,
                    name=instrument.get("name", ticker),
                    isin=instrument.get("isin"),
                    quantity=qty,
                    price=price,
                    currency=instrument.get("currency", ""),
                    fx_rate=fx_rate,
                    total_account_currency=net_value,
                ))
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                logger.warning("Skipping order (ticker=%s): %s", ticker, exc)
        logger.info("Retrieved %d filled orders from T212 API", len(result))
        return result

    def get_dividends(self, stop_before: Optional[date] = None) -> list[T212Dividend]:
        """Fetch all historical dividend payments and return as T212Dividend objects.

        ``stop_before``: stop paginating when the oldest item on a page has a
        date before this date.
        """
        logger.info("Fetching T212 dividend history (all pages)...")

        def _div_date(item: dict) -> Optional[date]:
            date_str = item.get("paidOn", "")
            return parse_t212_date(date_str) if date_str else None

        raw = self._paginate(
            "/api/v0/equity/history/dividends",
            self._history_limiter,
            stop_before=stop_before,
            date_fn=_div_date,
        )
        result = []
        for item in raw:
            instrument = item.get("instrument") or {}
            ticker = item.get("ticker") or instrument.get("ticker", "")
            if not ticker:
                continue
            try:
                paid_on = parse_t212_date(item.get("paidOn", ""))
                amount_raw = item.get("amount")
                amount = Decimal(str(amount_raw)) if amount_raw is not None else Decimal("0")
                gross_raw = item.get("grossAmountPerShare")
                gross = Decimal(str(gross_raw)) if gross_raw is not None else None
                result.append(T212Dividend(
                    paid_on=paid_on,
                    ticker=ticker,
                    name=instrument.get("name", ticker),
                    isin=instrument.get("isin"),
                    amount_account_currency=amount,
                    gross_per_share=gross,
                    withholding_tax=None,  # T212 dividend endpoint doesn't expose WHT
                    instrument_currency=item.get("tickerCurrency", item.get("currency", "")),
                    fx_rate=None,
                ))
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                logger.warning("Skipping dividend (ticker=%s): %s", ticker, exc)
        logger.info("Retrieved %d dividends from T212 API", len(result))
        return result


