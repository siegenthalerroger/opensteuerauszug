"""Parser for Trading212 transaction CSV exports."""
import csv
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from ._models import T212CashTransaction, T212Dividend, T212Order, parse_t212_date

logger = logging.getLogger(__name__)

BUY_ACTIONS = {"Market buy", "Limit buy", "Stop buy"}
SELL_ACTIONS = {"Market sell", "Limit sell", "Stop sell", "Stop limit sell"}
SPLIT_OPEN_ACTIONS = {"Stock split open"}
SPLIT_CLOSE_ACTIONS = {"Stock split close"}
# T212 exports dividends with optional sub-type suffixes, e.g.:
#   "Dividend (Ordinary)", "Dividend (Dividends paid by us corporations)", "Dividend (Bonus)"
# Match by prefix so new sub-types are handled automatically.
DIVIDEND_ACTION_PREFIX = "Dividend"
CASH_INCOME_ACTIONS = {"Interest on cash", "Lending interest"}
IGNORED_ACTIONS = {
    "Deposit", "Withdrawal",
    "Currency conversion", "Market order", "Spending cashback",
    # Cash adjustments (WHT refunds, interest reversals) — no ticker, not attributable
    "Result adjustment",
}

# Map from T212 CSV column header → internal field name
COLUMN_ALIASES: dict[str, str] = {
    "Action": "action",
    "Time": "time",
    "ISIN": "isin",
    "Ticker": "ticker",
    "Name": "name",
    "No. of shares": "quantity",
    "Price / share": "price",
    "Currency (Price / share)": "price_currency",
    "Exchange rate": "fx_rate",
    "Total": "total",
    "Currency (Total)": "total_currency",
    "Withholding tax": "withholding_tax",
    "Currency (Withholding tax)": "withholding_tax_currency",
    "Result": "result",
    "Currency (Result)": "result_currency",
    "Notes": "notes",
}

REQUIRED_COLUMNS = {"action", "time"}


class T212CsvParser:
    """
    Parses a Trading212 transaction history CSV export.

    The CSV is expected to contain at least 'Action' and 'Time' columns.
    Unknown actions are logged and skipped.
    """

    def __init__(self, csv_path: str):
        self._path = csv_path

    def parse(self) -> tuple[list[T212Order], list[T212Dividend], list[T212CashTransaction]]:
        """Parse the CSV and return (orders, dividends, cash_transactions)."""
        orders: list[T212Order] = []
        dividends: list[T212Dividend] = []
        cash_transactions: list[T212CashTransaction] = []

        with open(self._path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError(f"CSV file '{self._path}' appears to be empty.")

            col_map = _build_column_map(reader.fieldnames)
            _validate_required_columns(col_map, self._path)

            for row_num, raw_row in enumerate(reader, start=2):
                row = {col_map[k]: v for k, v in raw_row.items() if k in col_map}
                action = (row.get("action") or "").strip()
                if not action:
                    continue

                if action in BUY_ACTIONS or action in SELL_ACTIONS:
                    order = _parse_order(action, row, row_num, self._path)
                    if order is not None:
                        orders.append(order)
                elif action in SPLIT_OPEN_ACTIONS or action in SPLIT_CLOSE_ACTIONS:
                    order = _parse_split(action, row, row_num, self._path)
                    if order is not None:
                        orders.append(order)
                elif action.startswith(DIVIDEND_ACTION_PREFIX):
                    div = _parse_dividend(row, row_num, self._path)
                    if div is not None:
                        dividends.append(div)
                elif action in CASH_INCOME_ACTIONS:
                    tx = _parse_cash_transaction(action, row, row_num, self._path)
                    if tx is not None:
                        cash_transactions.append(tx)
                elif action not in IGNORED_ACTIONS:
                    logger.debug("CSV row %d: unknown action '%s', skipping", row_num, action)

        logger.info(
            "Parsed T212 CSV '%s': %d orders, %d dividends, %d cash transactions",
            self._path, len(orders), len(dividends), len(cash_transactions),
        )
        return orders, dividends, cash_transactions


def _build_column_map(fieldnames: list[str]) -> dict[str, str]:
    """Map raw CSV headers to internal field names."""
    result = {}
    for raw_header in fieldnames:
        if raw_header in COLUMN_ALIASES:
            result[raw_header] = COLUMN_ALIASES[raw_header]
    return result


def _validate_required_columns(col_map: dict[str, str], path: str) -> None:
    present = set(col_map.values())
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise ValueError(
            f"CSV file '{path}' is missing required columns: {missing}. "
            f"Found columns map to: {present}"
        )


def _parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace(",", ""))
    except InvalidOperation:
        return None



def _parse_order(action: str, row: dict, row_num: int, path: str) -> Optional[T212Order]:
    side = "BUY" if action in BUY_ACTIONS else "SELL"
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker, skipping", row_num, path)
        return None
    try:
        filled_at = parse_t212_date(row.get("time", ""))
        qty = _parse_decimal(row.get("quantity"))
        price = _parse_decimal(row.get("price"))
        if qty is None or price is None:
            logger.warning("CSV row %d (%s): missing quantity or price for %s, skipping", row_num, path, ticker)
            return None
        total_raw = _parse_decimal(row.get("total"))
        total = total_raw if total_raw is not None else Decimal("0")
        return T212Order(
            filled_at=filled_at,
            side=side,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            quantity=qty,
            price=price,
            currency=(row.get("price_currency") or "").strip(),
            fx_rate=_parse_decimal(row.get("fx_rate")),
            total_account_currency=total,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning("CSV row %d (%s): error parsing order for %s: %s", row_num, path, ticker, exc)
        return None


def _parse_dividend(row: dict, row_num: int, path: str) -> Optional[T212Dividend]:
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker for dividend, skipping", row_num, path)
        return None
    try:
        paid_on = parse_t212_date(row.get("time", ""))
        amount_raw = _parse_decimal(row.get("total"))
        amount = amount_raw if amount_raw is not None else Decimal("0")
        return T212Dividend(
            paid_on=paid_on,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            amount_account_currency=amount,
            gross_per_share=_parse_decimal(row.get("price")),  # "Price / share" = gross per share
            withholding_tax=_parse_decimal(row.get("withholding_tax")),
            instrument_currency=(row.get("price_currency") or row.get("total_currency") or "").strip(),
            fx_rate=_parse_decimal(row.get("fx_rate")),
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning("CSV row %d (%s): error parsing dividend for %s: %s", row_num, path, ticker, exc)
        return None


def _parse_split(action: str, row: dict, row_num: int, path: str) -> Optional[T212Order]:
    """Parse a stock-split row as a quantity-only mutation (no unit price)."""
    side = "BUY" if action in SPLIT_OPEN_ACTIONS else "SELL"
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker for stock split, skipping", row_num, path)
        return None
    try:
        filled_at = parse_t212_date(row.get("time", ""))
        qty = _parse_decimal(row.get("quantity"))
        if qty is None:
            logger.warning("CSV row %d (%s): missing quantity for stock split %s, skipping", row_num, path, ticker)
            return None
        return T212Order(
            filled_at=filled_at,
            side=side,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            quantity=qty,
            price=Decimal("0"),  # No meaningful unit price for a split
            currency=(row.get("price_currency") or "").strip(),
            fx_rate=None,
            total_account_currency=Decimal("0"),
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning("CSV row %d (%s): error parsing stock split for %s: %s", row_num, path, ticker, exc)
        return None


def _parse_cash_transaction(
    action: str, row: dict, row_num: int, path: str
) -> Optional[T212CashTransaction]:
    """Parse an interest-on-cash or lending-interest row."""
    try:
        tx_date = parse_t212_date(row.get("time", ""))
        amount = _parse_decimal(row.get("total"))
        currency = (row.get("total_currency") or "").strip()
        if amount is None or not currency:
            logger.warning(
                "CSV row %d (%s): missing amount or currency for '%s', skipping",
                row_num, path, action,
            )
            return None
        return T212CashTransaction(
            transaction_date=tx_date,
            action=action,
            amount=amount,
            currency=currency,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning("CSV row %d (%s): error parsing cash transaction: %s", row_num, path, exc)
        return None
