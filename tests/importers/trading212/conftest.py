"""Shared fixtures and factory helpers for Trading212 importer tests."""
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.config.models import Trading212AccountSettings
from opensteuerauszug.importers.trading212._models import T212Dividend, T212Order


# ---------------------------------------------------------------------------
# CSV helper constants / functions
# ---------------------------------------------------------------------------

T212_CSV_HEADERS = [
    "Action", "Time", "ISIN", "Ticker", "Name",
    "No. of shares", "Price / share", "Currency (Price / share)",
    "Exchange rate", "Total", "Currency (Total)",
    "Withholding tax", "Currency (Withholding tax)", "Result", "Currency (Result)",
]


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write a Trading212-format CSV to *path*."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=T212_CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Object factories
# ---------------------------------------------------------------------------

def make_settings(**kwargs) -> Trading212AccountSettings:
    defaults = dict(
        account_number="T212-TEST",
        broker_name="trading212",
        account_name_alias="test",
        full_name="Test User",
        account_currency="EUR",
        country="GB",
        ignore_crypto=True,
    )
    defaults.update(kwargs)
    return Trading212AccountSettings(**defaults)


def make_order(
    side="BUY",
    ticker="AAPL_US_EQ",
    isin="US0378331005",
    quantity="10",
    price="150.00",
    currency="USD",
    filled_at=date(2024, 3, 15),
    fx_rate="0.92",
    total="1380.00",
    name="Apple Inc",
) -> T212Order:
    return T212Order(
        filled_at=filled_at,
        side=side,
        ticker=ticker,
        name=name,
        isin=isin,
        quantity=Decimal(quantity),
        price=Decimal(price),
        currency=currency,
        fx_rate=Decimal(fx_rate),
        total_account_currency=Decimal(total),
    )


def make_dividend(
    ticker="AAPL_US_EQ",
    isin="US0378331005",
    amount="5.00",
    paid_on=date(2024, 5, 16),
    instrument_currency="USD",
    withholding_tax=None,
    fx_rate="0.92",
) -> T212Dividend:
    return T212Dividend(
        paid_on=paid_on,
        ticker=ticker,
        name="Apple Inc",
        isin=isin,
        amount_account_currency=Decimal(amount),
        gross_per_share=Decimal("0.25"),
        withholding_tax=Decimal(withholding_tax) if withholding_tax is not None else None,
        instrument_currency=instrument_currency,
        fx_rate=Decimal(fx_rate) if fx_rate is not None else None,
    )
