"""T212-to-eCH-0196 converters for the Trading212 importer."""
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from opensteuerauszug.model.ech0196 import (
    CurrencyId, SecurityCategory, SecurityPayment, SecurityStock,
)

from ._models import T212Dividend, T212Order

logger = logging.getLogger(__name__)

# Mapping from T212 /instruments type strings to eCH-0196 SecurityCategory values.
T212_TYPE_TO_ECH: Dict[str, SecurityCategory] = {
    "STOCK": "SHARE",
    "ETF": "FUND",
    "BOND": "BOND",
    "REIT": "FUND",
    "CRYPTO": "OTHER",
    "CRYPTOCURRENCY": "OTHER",  # /instruments endpoint may return either form
    "FOREX": "OTHER",
    "WARRANT": "OTHER",
    "CORPACT": "OTHER",
    "CVR": "OTHER",
    "INDEX": "OTHER",
    "FUTURES": "OTHER",
}

_CRYPTO_TYPES = frozenset({"CRYPTO", "CRYPTOCURRENCY"})


def _orders_to_mutation_stocks(
    orders: List[T212Order], currency: str
) -> List[SecurityStock]:
    """Convert a list of T212Order objects into SecurityStock mutation entries."""
    stocks = []
    for order in orders:
        # For BUY: positive quantity; for SELL: negative quantity
        qty = order.quantity if order.side == "BUY" else -order.quantity
        stocks.append(SecurityStock(
            referenceDate=order.filled_at,
            mutation=True,
            quotationType="PIECE",
            quantity=qty,
            balanceCurrency=CurrencyId(order.currency or currency),
            name=f"{order.side} {order.ticker}",
            unitPrice=order.price if order.price else None,
            exchangeRate=order.fx_rate,
        ))
    return stocks


def _dividend_to_payment(div: T212Dividend, currency: str) -> SecurityPayment:
    """Convert a T212Dividend to a SecurityPayment."""
    return SecurityPayment(
        paymentDate=div.paid_on,
        quotationType="PIECE",
        quantity=Decimal("0"),  # Not reported by T212; set to 0
        amountCurrency=CurrencyId(div.instrument_currency or currency),
        amount=div.amount_account_currency,
        amountPerUnit=div.gross_per_share,
        exchangeRate=div.fx_rate,
        withHoldingTaxClaim=div.withholding_tax,
        name=f"Dividend {div.ticker}",
    )


def _extract_instrument_info(
    ticker: str,
    orders: List[T212Order],
    dividends: List[T212Dividend],
    sec_type_override: Optional[str] = None,
) -> Tuple[Optional[str], str, str, str]:
    """Return (isin, name, sec_type, currency) for an instrument.

    ``sec_type_override`` should come from the /instruments metadata endpoint
    when available (e.g. "ETF", "CRYPTOCURRENCY").  Falls back to "STOCK".
    """
    isin: Optional[str] = None
    name = ticker
    sec_type = sec_type_override or "STOCK"
    currency = ""

    for o in orders:
        if not isin and o.isin:
            isin = o.isin
        if name == ticker and o.name and o.name != ticker:
            name = o.name
        if not currency and o.currency:
            currency = o.currency
        if isin and name != ticker and currency:
            break  # all fields resolved

    if not isin:
        for d in dividends:
            if d.isin:
                isin = d.isin
                break

    return isin, name, sec_type, currency


def _remap_tickers_via_isin(
    orders: List[T212Order],
    dividends: List[T212Dividend],
    isin_to_api_ticker: Dict[str, str],
) -> None:
    """Remap CSV short tickers to API qualified tickers using ISIN as bridge.

    CSV exports use short tickers (e.g. ``INTC``) while the T212 API uses
    qualified tickers (e.g. ``INTC_US_EQ``).  The ``/instruments`` endpoint
    provides both ISIN and API ticker, allowing us to bridge the two formats.

    Mutates ``orders`` and ``dividends`` in place.
    """
    remapped = 0
    for order in orders:
        if order.isin and order.isin in isin_to_api_ticker:
            api_ticker = isin_to_api_ticker[order.isin]
            if order.ticker != api_ticker:
                logger.debug(
                    "Remapping order ticker %s → %s (ISIN %s)",
                    order.ticker, api_ticker, order.isin,
                )
                order.ticker = api_ticker
                remapped += 1
    for div in dividends:
        if div.isin and div.isin in isin_to_api_ticker:
            api_ticker = isin_to_api_ticker[div.isin]
            if div.ticker != api_ticker:
                logger.debug(
                    "Remapping dividend ticker %s → %s (ISIN %s)",
                    div.ticker, api_ticker, div.isin,
                )
                div.ticker = api_ticker
                remapped += 1
    if remapped:
        logger.info("Remapped %d CSV tickers to API format via ISIN", remapped)
