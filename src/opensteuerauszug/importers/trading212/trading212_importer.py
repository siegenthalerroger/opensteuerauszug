"""Trading212 importer: supports both live API mode and CSV file mode."""
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from opensteuerauszug.config.models import Trading212AccountSettings
from opensteuerauszug.core.position_reconciler import PositionReconciler
from opensteuerauszug.model.critical_warning import CriticalWarning, CriticalWarningCategory
from opensteuerauszug.model.ech0196 import (
    BankAccount, BankAccountName, BankAccountNumber, BankAccountPayment,
    BankAccountTaxValue, Client, ClientNumber, CurrencyId, Depot, DepotNumber,
    Institution, ListOfBankAccounts, ListOfSecurities, Security,
    SecurityPayment, SecurityStock, TaxStatement,
)

from ._converters import (  # noqa: F401 (re-exported)
    T212_TYPE_TO_ECH, _CRYPTO_TYPES,
    _dividend_to_payment, _extract_instrument_info,
    _orders_to_mutation_stocks, _remap_tickers_via_isin,
)
from ._models import T212CashTransaction, T212Dividend, T212Order
from ._utils import _country_from_isin, _validate_isin  # noqa: F401 (re-exported)
from .api_client import T212ApiClient
from .csv_parser import T212CsvParser

logger = logging.getLogger(__name__)


class Trading212Importer:
    """
    Imports Trading212 account data for a given tax period.

    Supports three modes detected from the ``input_path`` argument and
    account configuration:

    - **CSV mode**: ``input_path`` is a CSV file and no ``api_key`` is
      configured.  Transactions parsed from the CSV export.
    - **Hybrid mode**: ``input_path`` is a CSV file and an ``api_key``
      is configured.  Transactions from CSV (preserving WHT and FX
      rates), positions and instrument types from the live API.
    - **API mode**: ``input_path`` is an existing directory; the importer
      calls the T212 REST API for all data.
    """

    def __init__(
        self,
        period_from: date,
        period_to: date,
        account_settings_list: List[Trading212AccountSettings],
    ):
        self.period_from = period_from
        self.period_to = period_to
        self._settings: Optional[Trading212AccountSettings] = (
            account_settings_list[0] if account_settings_list else None
        )
        if not account_settings_list:
            logger.warning("Trading212Importer initialised with no account settings.")
        elif len(account_settings_list) > 1:
            logger.warning(
                "Multiple Trading212 account configurations provided (%d); "
                "only the first account ('%s') will be used.",
                len(account_settings_list),
                account_settings_list[0].account_number,
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def import_from(self, input_path: str) -> TaxStatement:
        """
        Detect mode and import data.

        Args:
            input_path: Path to a CSV file (CSV mode) or any existing
                        directory (API mode).

        Returns:
            A populated ``TaxStatement``.
        """
        p = Path(input_path)
        if p.is_file():
            if self._settings and self._settings.api_key:
                logger.info(
                    "Trading212 hybrid mode (CSV + API): %s", input_path
                )
                return self._import_hybrid(str(p))
            logger.info("Trading212 CSV mode: %s", input_path)
            return self._import_csv(str(p))
        elif p.is_dir():
            logger.info("Trading212 API mode (directory: %s)", input_path)
            return self._import_api()
        else:
            raise ValueError(
                f"input_path '{input_path}' must be an existing file (CSV mode) "
                "or an existing directory (API mode)."
            )

    # ------------------------------------------------------------------
    # CSV mode
    # ------------------------------------------------------------------

    def _import_csv(self, path: str) -> TaxStatement:
        parser = T212CsvParser(path)
        orders, dividends, cash_transactions = parser.parse()
        return self._build_tax_statement(
            orders, dividends, current_positions=None,
            cash_transactions=cash_transactions,
        )

    # ------------------------------------------------------------------
    # Hybrid mode (CSV transactions + API positions & instrument types)
    # ------------------------------------------------------------------

    def _import_hybrid(self, csv_path: str) -> TaxStatement:
        """CSV for orders/dividends (with WHT + FX), API for positions + types."""
        parser = T212CsvParser(csv_path)
        orders, dividends, cash_transactions = parser.parse()

        client = T212ApiClient(self._settings.api_key, self._settings.api_secret, max_retries=self._settings.api_max_retries)

        isin_to_api_ticker: Dict[str, str] = {}
        extra_warnings: List[CriticalWarning] = []
        try:
            instrument_types, isin_to_api_ticker = client.get_instruments_extended()
        except Exception as exc:
            logger.warning(
                "Hybrid mode: could not fetch instrument types: %s; "
                "all instruments will default to STOCK.",
                exc,
            )
            instrument_types = {}
            extra_warnings.append(CriticalWarning(
                category=CriticalWarningCategory.OTHER,
                message=(
                    f"Instrument type metadata could not be fetched from the Trading212 API ({exc}). "
                    f"All securities will be reported as SHARE (stock). "
                    f"ETFs, bonds, and other asset classes may be misclassified, "
                    f"which can affect dividend and wealth-tax treatment. "
                    f"Check your API key scopes (Instruments metadata) or switch to hybrid mode."
                ),
                source="Trading212Importer",
            ))

        current_positions = client.get_current_positions()

        # Fetch the full API order history for position reconstruction.
        # CSV exports only cover the requested tax period; backward synthesis
        # from today's live position requires ALL intermediate mutations to be
        # accurate for prior-year statements.
        api_all_orders: Optional[List] = None
        try:
            logger.info(
                "Hybrid mode: fetching full API order history for position reconstruction..."
            )
            api_all_orders = client.get_orders()
        except Exception as exc:
            logger.warning(
                "Hybrid mode: could not fetch full order history from API (%s); "
                "position synthesis will fall back to CSV-only mutations — "
                "prior-year closing balances may be incorrect.",
                exc,
            )
            extra_warnings.append(CriticalWarning(
                category=CriticalWarningCategory.OTHER,
                message=(
                    f"Full order history could not be fetched from the Trading212 API ({exc}). "
                    f"Position synthesis will fall back to CSV transaction history only. "
                    f"Opening and closing balances for years not fully covered by the CSV export "
                    f"may be inaccurate."
                ),
                source="Trading212Importer",
            ))

        # Remap CSV short tickers to API qualified tickers via ISIN so that
        # position lookups in _build_tax_statement match the API ticker format.
        if isin_to_api_ticker:
            _remap_tickers_via_isin(orders, dividends, isin_to_api_ticker)

        return self._build_tax_statement(
            orders, dividends, current_positions, instrument_types=instrument_types,
            cash_transactions=cash_transactions,
            reconciliation_orders=api_all_orders,
            extra_warnings=extra_warnings,
        )

    # ------------------------------------------------------------------
    # API mode
    # ------------------------------------------------------------------

    def _import_api(self) -> TaxStatement:
        if self._settings is None or not self._settings.api_key:
            raise ValueError(
                "Trading212 API mode requires an 'api_key' in the account "
                "configuration. Add 'api_key = \"...\"' under "
                "[brokers.trading212.accounts.<name>] in config.toml."
            )
        client = T212ApiClient(self._settings.api_key, self._settings.api_secret, max_retries=self._settings.api_max_retries)

        logger.info("Fetching Trading212 account summary...")
        summary = client.get_account_summary()
        logger.info(
            "Account %s (%s)", summary.get("id", "?"), summary.get("currency", "?")
        )

        # Instrument type metadata (STOCK, ETF, CRYPTOCURRENCY, …) — best-effort.
        extra_warnings: List[CriticalWarning] = []
        try:
            instrument_types = client.get_instruments()
        except Exception as exc:
            logger.warning(
                "Could not fetch instrument types from /instruments: %s; "
                "all instruments will default to STOCK.",
                exc,
            )
            instrument_types = {}
            extra_warnings.append(CriticalWarning(
                category=CriticalWarningCategory.OTHER,
                message=(
                    f"Instrument type metadata could not be fetched from the Trading212 API ({exc}). "
                    f"All securities will be reported as SHARE (stock). "
                    f"ETFs, bonds, and other asset classes may be misclassified, "
                    f"which can affect dividend and wealth-tax treatment. "
                    f"Check your API key scopes (Instruments metadata)."
                ),
                source="Trading212Importer",
            ))

        orders = client.get_orders(stop_before=self.period_from)
        dividends = client.get_dividends(stop_before=self.period_from)
        current_positions = client.get_current_positions()
        # API mode has no cash transaction history; pass empty list.
        return self._build_tax_statement(
            orders, dividends, current_positions, instrument_types=instrument_types,
            cash_transactions=[],
            extra_warnings=extra_warnings,
        )

    # ------------------------------------------------------------------
    # Core assembly
    # ------------------------------------------------------------------

    def _build_tax_statement(
        self,
        all_orders: List[T212Order],
        all_dividends: List[T212Dividend],
        current_positions: Optional[List[dict]],
        instrument_types: Optional[Dict[str, str]] = None,
        cash_transactions: Optional[List[T212CashTransaction]] = None,
        reconciliation_orders: Optional[List[T212Order]] = None,
        extra_warnings: Optional[List[CriticalWarning]] = None,
    ) -> TaxStatement:
        """
        Build a TaxStatement from the provided orders and dividends.

        ``all_orders`` are the CSV-derived orders used for tax output.
        ``reconciliation_orders`` (optional) should be the full API order
        history used solely for position reconstruction; when supplied, backward
        synthesis from today's live position is accurate across multiple years.
        Only mutations within [period_from, period_to] appear in the output.
        """
        settings = self._settings
        account_currency = settings.account_currency if settings else "EUR"
        country = settings.country if settings else "GB"
        ignore_crypto = settings.ignore_crypto if settings else True
        depot_id = settings.account_number if settings else "T212"

        # Index orders and dividends by ticker
        orders_by_ticker: Dict[str, List[T212Order]] = defaultdict(list)
        for o in all_orders:
            orders_by_ticker[o.ticker].append(o)

        dividends_by_ticker: Dict[str, List[T212Dividend]] = defaultdict(list)
        for d in all_dividends:
            dividends_by_ticker[d.ticker].append(d)

        # Index full API order history for position reconstruction (hybrid mode).
        # These orders span the complete account lifetime and allow backward
        # synthesis from today's live position to any prior year-start date.
        recon_orders_by_ticker: Optional[Dict[str, List[T212Order]]] = None
        if reconciliation_orders is not None:
            recon_orders_by_ticker = defaultdict(list)
            for o in reconciliation_orders:
                recon_orders_by_ticker[o.ticker].append(o)

        # Build current-position lookup for API/hybrid mode.
        # When current_positions is not None (API/hybrid), tickers absent from
        # the list are known to have 0 shares — not unknown.
        current_pos_by_ticker: Optional[Dict[str, dict]] = None
        if current_positions is not None:
            current_pos_by_ticker = {}
            for pos in current_positions:
                instrument = pos.get("instrument") or {}
                ticker = pos.get("ticker") or instrument.get("ticker", "")
                if ticker:
                    current_pos_by_ticker[ticker] = pos

        all_tickers = set(orders_by_ticker) | set(dividends_by_ticker)
        instruments_dict = instrument_types or {}
        statement_warnings: List[CriticalWarning] = list(extra_warnings or [])
        securities: List[Security] = []

        for position_counter, ticker in enumerate(sorted(all_tickers), start=1):
            sec, ticker_warnings = self._process_ticker(
                ticker=ticker,
                ticker_orders=orders_by_ticker.get(ticker, []),
                ticker_dividends=dividends_by_ticker.get(ticker, []),
                current_pos_by_ticker=current_pos_by_ticker,
                recon_orders_by_ticker=recon_orders_by_ticker,
                instruments_dict=instruments_dict,
                account_currency=account_currency,
                country=country,
                ignore_crypto=ignore_crypto,
                position_id=position_counter,
            )
            statement_warnings.extend(ticker_warnings)
            if sec is not None:
                securities.append(sec)

        depot = Depot(
            depotNumber=DepotNumber(depot_id[:40]),
            security=securities,
        )
        list_of_securities = ListOfSecurities(depot=[depot]) if securities else None
        list_of_bank_accounts = self._build_bank_accounts(
            cash_transactions or [], depot_id,
        )
        statement = TaxStatement(
            minorVersion=1,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
            listOfSecurities=list_of_securities,
            listOfBankAccounts=list_of_bank_accounts,
            institution=Institution(name="Trading212"),
            client=[Client(clientNumber=ClientNumber(depot_id))],
        )
        statement.critical_warnings.extend(statement_warnings)
        return statement

    def _process_ticker(
        self,
        ticker: str,
        ticker_orders: List[T212Order],
        ticker_dividends: List[T212Dividend],
        current_pos_by_ticker: Optional[Dict[str, dict]],
        recon_orders_by_ticker: Optional[Dict[str, List[T212Order]]],
        instruments_dict: Dict[str, str],
        account_currency: str,
        country: str,
        ignore_crypto: bool,
        position_id: int,
    ) -> Tuple[Optional[Security], List[CriticalWarning]]:
        """Process a single ticker and return (Security | None, warnings).

        Returns ``None`` for the Security when the ticker is skipped (crypto
        filter or no output stocks within the tax period).
        """
        warnings: List[CriticalWarning] = []

        api_type = instruments_dict.get(ticker)
        isin, name, sec_type, instrument_currency = _extract_instrument_info(
            ticker, ticker_orders, ticker_dividends, sec_type_override=api_type
        )

        # Optionally skip crypto (catches both CRYPTO and CRYPTOCURRENCY)
        if ignore_crypto and sec_type.upper() in _CRYPTO_TYPES:
            logger.debug("Skipping CRYPTO instrument: %s", ticker)
            return None, warnings

        currency = instrument_currency or account_currency
        ech_category = T212_TYPE_TO_ECH.get(sec_type.upper(), "SHARE")

        # In API/hybrid mode (dict provided), tickers absent from positions are
        # known to have 0 shares. In CSV-only mode there is no position anchor.
        if current_pos_by_ticker is not None:
            current_pos = current_pos_by_ticker.get(ticker, {"quantity": 0})
        else:
            current_pos = None

        # Build SecurityStock mutation entries from CSV orders (tax period output)
        all_mutation_stocks = _orders_to_mutation_stocks(ticker_orders, currency)

        # Build full-history mutation stocks for position reconstruction.
        # In hybrid mode these come from the complete API order history so
        # that backward synthesis from today's live position is accurate
        # even when the CSV export only covers a single year.
        # Fall back to CSV mutations when the API returned no orders for
        # this ticker (e.g. stock only traded in the CSV period with no
        # subsequent trades recorded in the API history yet).
        recon_mutation_stocks: Optional[List[SecurityStock]] = None
        if recon_orders_by_ticker is not None:
            recon_ticker_orders = recon_orders_by_ticker.get(ticker, [])
            if recon_ticker_orders:
                recon_mutation_stocks = _orders_to_mutation_stocks(
                    recon_ticker_orders, currency
                )

        # Add boundary balances (year-start, year-end)
        all_stocks_with_boundaries = self._add_boundary_balances(
            ticker=ticker,
            all_mutation_stocks=all_mutation_stocks,
            recon_mutation_stocks=recon_mutation_stocks,
            currency=currency,
            current_position=current_pos,
        )

        for s in all_stocks_with_boundaries:
            if not s.mutation and s.quantity < 0:
                warnings.append(CriticalWarning(
                    category=CriticalWarningCategory.NEGATIVE_BALANCE,
                    message=(
                        f"Security {ticker} has a negative {s.name} of {s.quantity}. "
                        f"The CSV export may be missing earlier BUY records "
                        f"(e.g. a position acquired via a corporate action or warrant grant). "
                        f"The reported balance may be incorrect."
                    ),
                    source="Trading212Importer",
                    identifier=ticker,
                ))

        # Detect synthesis failure: orders exist but no boundary balances produced.
        has_boundaries = any(not s.mutation for s in all_stocks_with_boundaries)
        if not has_boundaries and ticker_orders:
            warnings.append(CriticalWarning(
                category=CriticalWarningCategory.OTHER,
                message=(
                    f"Opening and closing balances could not be synthesized for {ticker}. "
                    f"The tax statement will not include opening/closing quantity balances "
                    f"for this security, which may affect the wealth tax assessment (Vermögenssteuerwert). "
                    f"Ensure the CSV export covers the full account history from inception, "
                    f"or use hybrid mode for accurate position anchoring."
                ),
                source="Trading212Importer",
                identifier=ticker,
            ))

        # Filter: only keep mutations within the tax period for the output
        output_stocks = [
            s for s in all_stocks_with_boundaries
            if not s.mutation
            or (self.period_from <= s.referenceDate <= self.period_to)
        ]

        if not output_stocks:
            logger.debug("No stock entries for %s in tax period, skipping.", ticker)
            return None, warnings

        payments = [
            _dividend_to_payment(d, currency)
            for d in ticker_dividends
            if self.period_from <= d.paid_on <= self.period_to
        ]

        sec = Security(
            positionId=position_id,
            country=_country_from_isin(isin) or country,
            currency=CurrencyId(currency),
            quotationType="PIECE",
            securityCategory=ech_category,
            securityName=name,
            isin=_validate_isin(isin),
            symbol=ticker,
            stock=output_stocks,
            payment=payments,
        )
        return sec, warnings

    # ------------------------------------------------------------------
    # Position reconstruction helpers
    # ------------------------------------------------------------------

    def _add_boundary_balances(
        self,
        ticker: str,
        all_mutation_stocks: List[SecurityStock],
        currency: str,
        current_position: Optional[dict],
        recon_mutation_stocks: Optional[List[SecurityStock]] = None,
    ) -> List[SecurityStock]:
        """
        Add year-start and year-end balance entries to the stock list.

        Strategy:
        - API/hybrid mode (``current_position`` provided): seed the reconciler
          with today's live balance as a known anchor, then synthesize backward.
          ``recon_mutation_stocks`` should supply the complete order history so
          that backward synthesis across multiple years is accurate; when None
          it falls back to ``all_mutation_stocks``.
        - CSV mode (no ``current_position``): use ``assume_zero_if_no_balances``
          and synthesize forward from a zero initial position.
        """
        today = date.today()
        # Use full-history mutations for reconciliation if available; fall back
        # to the CSV-derived mutations when not in hybrid mode.
        stocks_for_recon = (
            recon_mutation_stocks if recon_mutation_stocks is not None
            else all_mutation_stocks
        )
        anchor_stocks = list(stocks_for_recon)

        if current_position is not None:
            # API/hybrid mode: add today's live position as a balance anchor
            qty_raw = current_position.get("quantity", 0)
            qty = Decimal(str(qty_raw)) if qty_raw else Decimal("0")
            anchor_stocks.append(SecurityStock(
                referenceDate=today,
                mutation=False,
                quotationType="PIECE",
                quantity=qty,
                balanceCurrency=CurrencyId(currency),
                name=f"Current Position {today}",
            ))

        assume_zero = current_position is None  # CSV mode: no balance anchor

        reconciler = PositionReconciler(anchor_stocks, identifier=ticker)

        # Synthesize year-start balance (position at start of period_from = end of prior year)
        start_qty = reconciler.synthesize_position_at_date(
            self.period_from, assume_zero_if_no_balances=assume_zero
        )

        # Result stocks: CSV mutations only — the reconciliation anchor (today's
        # live position and full-history API orders) must not appear in output.
        result_stocks = list(all_mutation_stocks)

        if start_qty is not None:
            result_stocks.append(SecurityStock(
                referenceDate=self.period_from,
                mutation=False,
                quotationType="PIECE",
                quantity=start_qty.quantity,
                balanceCurrency=CurrencyId(currency),
                name="Opening Balance",
            ))
            # Compute year-end balance arithmetically:
            #   closing = opening + net mutations within [period_from, period_to]
            # This avoids a second PositionReconciler while producing the same result.
            period_net = sum(
                s.quantity for s in all_mutation_stocks
                if self.period_from <= s.referenceDate <= self.period_to
            )
            closing_qty = start_qty.quantity + period_net
            end_date = self.period_to + timedelta(days=1)
            result_stocks.append(SecurityStock(
                referenceDate=end_date,
                mutation=False,
                quotationType="PIECE",
                quantity=closing_qty,
                balanceCurrency=CurrencyId(currency),
                name="Closing Balance",
            ))
        else:
            # If the opening balance cannot be synthesized (e.g. no balance anchor and
            # no mutation history), the closing balance is also skipped — it cannot be
            # derived without a known starting point.
            logger.warning(
                "Could not synthesize year-start position for %s on %s; "
                "closing balance will also be omitted.",
                ticker, self.period_from,
            )

        return result_stocks

    # ------------------------------------------------------------------
    # Bank account (cash) helpers
    # ------------------------------------------------------------------

    def _build_bank_accounts(
        self,
        cash_transactions: List[T212CashTransaction],
        depot_id: str,
    ) -> Optional[ListOfBankAccounts]:
        """Build ListOfBankAccounts from cash transactions and configured balances.

        Aggregates daily interest/lending entries per currency into annual
        totals and creates one ``BankAccount`` per currency.
        """
        settings = self._settings

        # Filter to tax period
        period_txs = [
            tx for tx in cash_transactions
            if self.period_from <= tx.transaction_date <= self.period_to
        ]

        if not period_txs and not (settings and settings.cash_balances):
            return None

        # Aggregate per (currency, action) → total amount
        aggregated: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
        currencies_seen: set[str] = set()
        for tx in period_txs:
            aggregated[(tx.currency, tx.action)] += tx.amount
            currencies_seen.add(tx.currency)

        # Also include currencies from cash_balances config
        if settings and settings.cash_balances:
            currencies_seen.update(settings.cash_balances.keys())

        bank_accounts: List[BankAccount] = []
        for currency in sorted(currencies_seen):
            payments: List[BankAccountPayment] = []

            # Interest on cash
            interest_key = (currency, "Interest on cash")
            if interest_key in aggregated:
                payments.append(BankAccountPayment(
                    paymentDate=self.period_to,
                    name="Interest on cash",
                    amountCurrency=currency,
                    amount=aggregated[interest_key],
                ))

            # Lending interest
            lending_key = (currency, "Lending interest")
            if lending_key in aggregated:
                payments.append(BankAccountPayment(
                    paymentDate=self.period_to,
                    name="Share lending interest",
                    amountCurrency=currency,
                    amount=aggregated[lending_key],
                ))

            # Tax value (closing balance) from config
            tax_value = None
            if settings and settings.cash_balances and currency in settings.cash_balances:
                tax_value = BankAccountTaxValue(
                    referenceDate=self.period_to,
                    name="Closing Balance",
                    balanceCurrency=currency,
                    balance=settings.cash_balances[currency],
                )

            if not payments and tax_value is None:
                continue

            account_name = f"{depot_id} {currency} cash"
            account_number = f"{depot_id}-{currency}"
            bank_accounts.append(BankAccount(
                bankAccountName=BankAccountName(account_name[:40]),
                bankAccountNumber=BankAccountNumber(account_number[:32]),
                bankAccountCountry="CY",  # Trading212 is Cyprus-based
                bankAccountCurrency=CurrencyId(currency),
                payment=sorted(payments, key=lambda p: p.paymentDate),
                taxValue=tax_value,
            ))

        if not bank_accounts:
            return None

        return ListOfBankAccounts(bankAccount=bank_accounts)
