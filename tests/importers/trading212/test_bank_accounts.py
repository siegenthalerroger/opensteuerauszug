"""Tests for _build_bank_accounts: interest/lending aggregation, cash_balances config."""
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.importers.trading212._models import T212CashTransaction
from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer

from .conftest import make_settings


class TestBankAccountCreation:
    """_build_bank_accounts aggregates cash transactions into BankAccount objects."""

    def test_interest_aggregated_per_currency(self):
        """Daily interest entries are summed into one BankAccountPayment per currency."""
        cash_txs = [
            T212CashTransaction(date(2024, 3, 1), "Interest on cash", Decimal("0.10"), "CHF"),
            T212CashTransaction(date(2024, 3, 2), "Interest on cash", Decimal("0.15"), "CHF"),
            T212CashTransaction(date(2024, 6, 1), "Interest on cash", Decimal("0.03"), "USD"),
        ]
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        assert stmt.listOfBankAccounts is not None
        accounts = stmt.listOfBankAccounts.bankAccount
        assert len(accounts) == 2  # CHF and USD

        chf_acct = next(a for a in accounts if a.bankAccountCurrency == "CHF")
        usd_acct = next(a for a in accounts if a.bankAccountCurrency == "USD")

        assert len(chf_acct.payment) == 1
        assert chf_acct.payment[0].amount == Decimal("0.25")  # 0.10 + 0.15
        assert chf_acct.payment[0].name == "Interest on cash"

        assert len(usd_acct.payment) == 1
        assert usd_acct.payment[0].amount == Decimal("0.03")

    def test_lending_interest_creates_payment(self):
        """Lending interest creates a separate BankAccountPayment."""
        cash_txs = [
            T212CashTransaction(date(2024, 3, 1), "Interest on cash", Decimal("10.00"), "CHF"),
            T212CashTransaction(date(2024, 3, 1), "Lending interest", Decimal("5.00"), "CHF"),
        ]
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        chf_acct = stmt.listOfBankAccounts.bankAccount[0]
        assert len(chf_acct.payment) == 2
        interest = next(p for p in chf_acct.payment if p.name == "Interest on cash")
        lending = next(p for p in chf_acct.payment if p.name == "Share lending interest")
        assert interest.amount == Decimal("10.00")
        assert lending.amount == Decimal("5.00")

    def test_cash_balances_from_config(self):
        """cash_balances config creates BankAccountTaxValue on the BankAccount."""
        cash_txs = [
            T212CashTransaction(date(2024, 6, 1), "Interest on cash", Decimal("63.20"), "CHF"),
        ]
        settings = make_settings(cash_balances={"CHF": Decimal("5000.00"), "USD": Decimal("100.00")})
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        accounts = stmt.listOfBankAccounts.bankAccount
        chf_acct = next(a for a in accounts if a.bankAccountCurrency == "CHF")
        usd_acct = next(a for a in accounts if a.bankAccountCurrency == "USD")

        assert chf_acct.taxValue is not None
        assert chf_acct.taxValue.balance == Decimal("5000.00")
        assert chf_acct.taxValue.balanceCurrency == "CHF"

        # USD has no transactions but has a configured balance
        assert usd_acct.taxValue is not None
        assert usd_acct.taxValue.balance == Decimal("100.00")

    def test_no_cash_balances_config_means_no_tax_value(self):
        """Without cash_balances config, BankAccount has payments but no taxValue."""
        cash_txs = [
            T212CashTransaction(date(2024, 6, 1), "Interest on cash", Decimal("63.20"), "CHF"),
        ]
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        chf_acct = stmt.listOfBankAccounts.bankAccount[0]
        assert chf_acct.taxValue is None
        assert len(chf_acct.payment) == 1

    def test_no_cash_transactions_and_no_config_means_no_bank_accounts(self):
        """No cash transactions + no cash_balances → no ListOfBankAccounts."""
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=[])
        assert stmt.listOfBankAccounts is None

    def test_out_of_period_transactions_filtered(self):
        """Cash transactions outside the tax period are excluded."""
        cash_txs = [
            T212CashTransaction(date(2023, 12, 31), "Interest on cash", Decimal("1.00"), "CHF"),
            T212CashTransaction(date(2024, 6, 1), "Interest on cash", Decimal("2.00"), "CHF"),
            T212CashTransaction(date(2025, 1, 1), "Interest on cash", Decimal("3.00"), "CHF"),
        ]
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        chf_acct = stmt.listOfBankAccounts.bankAccount[0]
        assert chf_acct.payment[0].amount == Decimal("2.00")  # Only in-period

    def test_bank_account_metadata(self):
        """BankAccount has correct name, number, country, and currency."""
        cash_txs = [
            T212CashTransaction(date(2024, 6, 1), "Interest on cash", Decimal("1.00"), "CHF"),
        ]
        settings = make_settings()
        importer = Trading212Importer(
            period_from=date(2024, 1, 1),
            period_to=date(2024, 12, 31),
            account_settings_list=[settings],
        )
        stmt = importer._build_tax_statement([], [], current_positions=None, cash_transactions=cash_txs)

        acct = stmt.listOfBankAccounts.bankAccount[0]
        assert "CHF" in str(acct.bankAccountName)
        assert acct.bankAccountCountry == "CY"
        assert acct.bankAccountCurrency == "CHF"
