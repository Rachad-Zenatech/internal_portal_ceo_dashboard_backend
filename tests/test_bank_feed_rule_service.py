import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from models.bank_feed_rule_model import BankTransactionClassificationRequest
from models.general_ledger_model import GeneralLedgerTransaction
from services.bank_feed_rule_service import (
    _apply_actions,
    _condition_matches,
    _money_direction_condition_matches,
    _rule_matches,
    classify_bank_transaction,
    match_bank_feed_rule,
)
from services.business_contact_reference_service import (
    lookup_accounts_receivable_payment_contact,
)
from services.gl_account_suggestion_service import (
    merge_ai_account_review_suggestions,
    suggest_split_account,
)


def condition(rule_type, value, name=None):
    row = {"rule_type": rule_type, "value": value}
    if name:
        row["rule_type_name"] = name
    return row


def action(action_type, value):
    return {"action_type": action_type, "value": value}


def rule(rule_id, name, conditions, actions, is_and=True):
    return {
        "id": rule_id,
        "rule_name": name,
        "is_and_rule": is_and,
        "conditions": conditions,
        "actions": actions,
    }


class BankFeedConditionTests(unittest.TestCase):
    def test_transaction_type_controls_money_direction(self):
        out_condition = ["-1"]
        in_condition = ["1"]

        expense = BankTransactionClassificationRequest(
            transaction_type="Expense", amount=48.71
        )
        credit = BankTransactionClassificationRequest(
            transaction_type="Credit Card Credit", amount=-48.71
        )
        payment = BankTransactionClassificationRequest(
            transaction_type="Bill Payment (Check)", amount=187.00
        )

        self.assertTrue(_money_direction_condition_matches(expense, out_condition))
        self.assertTrue(_money_direction_condition_matches(credit, out_condition))
        self.assertFalse(_money_direction_condition_matches(credit, in_condition))
        self.assertTrue(_money_direction_condition_matches(payment, out_condition))

    def test_zero_amount_matches_neither_direction(self):
        txn = BankTransactionClassificationRequest(
            transaction_type="Expense", amount=0.0
        )
        self.assertFalse(_money_direction_condition_matches(txn, ["-1"]))
        self.assertFalse(_money_direction_condition_matches(txn, ["1"]))

    def test_negative_text_operators_check_every_value(self):
        txn = BankTransactionClassificationRequest(description="Amazon office supplies")
        does_not_contain = condition(
            1,
            {"operator": "does_not_contain", "values": ["FedEx", "Amazon"]},
            "DESCRIPTION CONTAINS",
        )
        not_equal = condition(
            1,
            {"operator": "not_equals", "values": ["FedEx", "Amazon office supplies"]},
            "DESCRIPTION CONTAINS",
        )
        self.assertFalse(_condition_matches(does_not_contain, txn))
        self.assertFalse(_condition_matches(not_equal, txn))

    def test_amount_operators_support_multiple_values(self):
        txn = BankTransactionClassificationRequest(amount=100.0)
        self.assertTrue(
            _condition_matches(
                condition(2, {"operator": "equals", "values": [50, 100]}), txn
            )
        )
        self.assertFalse(
            _condition_matches(
                condition(2, {"operator": "not_equals", "values": [50, 100]}),
                txn,
            )
        )

    def test_direction_is_stacked_with_or_search_conditions(self):
        qb_rule = rule(
            1,
            "Amazon",
            [condition(10, "-1", "FOR"), condition(6, "Amazon", "BANK TEXT CONTAINS")],
            [action(0, {"account_number": "6340", "account_name": "Office Supplies"})],
            is_and=False,
        )
        money_in = BankTransactionClassificationRequest(
            bank_text="Amazon", transaction_type="Deposit", amount=10
        )
        money_out = BankTransactionClassificationRequest(
            bank_text="Amazon", transaction_type="Expense", amount=10
        )
        self.assertFalse(_rule_matches(qb_rule, money_in))
        self.assertTrue(_rule_matches(qb_rule, money_out))

    def test_federal_express_alias_matches_fedex(self):
        qb_rule = rule(
            2,
            "FedEx",
            [condition(1, "Federal Express", "DESCRIPTION CONTAINS")],
            [action(0, "6380 Office Expense:Postage and Delivery")],
        )
        txn = BankTransactionClassificationRequest(description="FEDEX INTL FEE")
        self.assertTrue(_rule_matches(qb_rule, txn))

    def test_ar_customer_ignores_purchases_and_bankcard_1292(self):
        customer = {
            "account_number": "1100",
            "account_name": "Accounts Receivable",
            "display_name": "Garry Piltier",
        }
        with patch(
            "services.business_contact_reference_service.lookup_business_contact_reference",
            return_value=customer,
        ) as contact_lookup:
            self.assertIsNone(
                lookup_accounts_receivable_payment_contact(
                    "Garry Piltier", "Expense", "office purchase"
                )
            )
            self.assertIsNone(
                lookup_accounts_receivable_payment_contact(
                    "Garry Piltier", "Deposit", "BANKCARD-1292 merchant deposit"
                )
            )
            self.assertEqual(
                lookup_accounts_receivable_payment_contact(
                    "Garry Piltier", "Deposit", "customer receipt"
                ),
                customer,
            )
        contact_lookup.assert_called_once_with(
            "Garry Piltier", contact_type="customer"
        )


class BankFeedActionAndPrecedenceTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_actions_are_applied(self):
        result = _apply_actions(
            [
                action(0, {"account_number": "6340", "account_name": "Office Supplies"}),
                action(5, "Amazon"),
                action(9, "Office order"),
            ]
        )
        self.assertEqual(result["account_number"], "6340")
        self.assertEqual(result["payee"], "Amazon")
        self.assertEqual(result["memo"], "Office order")

    def test_empty_or_unsupported_rule_does_not_block_usable_rule(self):
        txn = BankTransactionClassificationRequest(description="Amazon")
        rules = [
            rule(3, "Empty", [condition(1, "Amazon")], []),
            rule(2, "Class only", [condition(1, "Amazon")], [action(2, "Operations")]),
            rule(
                1,
                "Usable",
                [condition(1, "Amazon")],
                [action(0, {"account_number": "6340", "account_name": "Office Supplies"})],
            ),
        ]
        matched = match_bank_feed_rule(txn, rules)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["matched_rule"].rule_name, "Usable")

    def test_unresolved_or_excluded_account_does_not_block_next_rule(self):
        txn = BankTransactionClassificationRequest(description="Amazon")
        rules = [
            rule(2, "Missing", [condition(1, "Amazon")], [action(0, "Missing Account")]),
            rule(1, "Valid", [condition(1, "Amazon")], [action(0, "6340 Office Supplies")]),
        ]
        coa = [{"account_number": "6340", "account_name": "Office Supplies"}]
        self.assertEqual(
            match_bank_feed_rule(txn, rules, coa_accounts=coa)["account_number"],
            "6340",
        )
        self.assertIsNone(
            match_bank_feed_rule(
                txn, rules, coa_accounts=coa, excluded_account_numbers=["6340"]
            )
        )

    def test_amazon_credit_card_credit_keeps_quickbooks_no_change(self):
        txn = GeneralLedgerTransaction(
            ledger_account_code="2010",
            ledger_account_name="Amex - 77002 (InterlinkOne)",
            split_account_code="1150",
            split_account_name="Supplies Inventory",
            transaction_type="Credit Card Credit",
            name="Amazon.AE (Sharjah, UAE)",
            memo="AMAZON (MARKET PLACEDUBAI XXXX2144",
            source_amount=Decimal("-101.84"),
            amount=Decimal("101.84"),
        )
        rules = [
            rule(
                852,
                "Amazon UAE Inventory Supplies",
                [condition(10, "-1"), condition(1, "UAE")],
                [action(0, {"account_number": "1150", "account_name": "Supplies Inventory"})],
            )
        ]
        coa = [
            {"account_number": "2010", "account_name": "Amex - 77002 (InterlinkOne)"},
            {"account_number": "1150", "account_name": "Supplies Inventory"},
            {"account_number": "1940", "account_name": "Product Development Costs"},
        ]

        suggestion = suggest_split_account(
            txn,
            coa,
            use_xgboost=False,
            bank_feed_rules=rules,
        )

        self.assertEqual(suggestion.rule, "quickbooks_rule")
        self.assertEqual(suggestion.suggested_account_number, "1150")
        self.assertFalse(suggestion.requires_manual_review)

    def test_hh_builders_payment_keeps_accounts_receivable_before_lookup(self):
        txn = GeneralLedgerTransaction(
            ledger_account_code="1000",
            ledger_account_name="BOA - 7458 (Pace Plus)",
            split_account_code="1100",
            split_account_name="Accounts Receivable",
            transaction_type="Payment",
            transaction_number="5044",
            name="H&H Builders",
            memo="H&H Builders",
            source_amount=Decimal("75.00"),
            amount=Decimal("75.00"),
        )
        coa = [
            {"account_number": "1000", "account_name": "BOA - 7458 (Pace Plus)"},
            {"account_number": "1100", "account_name": "Accounts Receivable"},
            {"account_number": "1120", "account_name": "Undeposited Funds"},
        ]
        with patch(
            "services.gl_account_suggestion_service.lookup_accounts_receivable_payment_contact",
            return_value={
                "account_number": "1100",
                "account_name": "Accounts Receivable",
                "display_name": "H&H Builders",
            },
        ):
            suggestion = suggest_split_account(
                txn,
                coa,
                use_xgboost=False,
                bank_feed_rules=[],
            )

        self.assertEqual(suggestion.rule, "accounts_receivable_contact")
        self.assertEqual(suggestion.suggested_account_number, "1100")
        self.assertFalse(suggestion.requires_manual_review)

    async def test_quickbooks_rule_precedes_ap_lookup_and_one_to_one(self):
        txn = BankTransactionClassificationRequest(
            description="Amazon", name="Amazon", transaction_type="Expense", amount=10
        )
        rules = [
            rule(
                1,
                "Amazon QB",
                [condition(10, "-1"), condition(1, "Amazon")],
                [action(0, {"account_number": "6340", "account_name": "Office Supplies"})],
            )
        ]
        with patch(
            "services.bank_feed_rule_service.lookup_accounts_receivable_payment_contact",
            return_value={"account_number": "1100", "account_name": "Accounts Receivable", "display_name": "Amazon"},
        ), patch(
            "services.bank_feed_rule_service.lookup_accounts_payable_payment_contact",
            return_value={"account_number": "2000", "account_name": "Accounts Payable", "display_name": "Amazon"},
        ), patch(
            "services.bank_feed_rule_service.lookup_account_split_mapping_async",
            new=AsyncMock(return_value={"split_account_number": "1940", "split_account_name": "Development"}),
        ):
            result = await classify_bank_transaction(txn, rules=rules)
        self.assertEqual(result.source, "quickbooks_rule")
        self.assertEqual(result.account_number, "6340")
        self.assertFalse(result.requires_ai_review)

    async def test_ar_customer_payment_precedes_one_to_one(self):
        txn = BankTransactionClassificationRequest(
            name="H&H Builders",
            description="H&H Builders",
            transaction_type="Payment",
            transaction_number="5044",
            amount=75,
        )
        with patch(
            "services.bank_feed_rule_service.lookup_accounts_receivable_payment_contact",
            return_value={
                "account_number": "1100",
                "account_name": "Accounts Receivable",
                "display_name": "H&H Builders",
            },
        ), patch(
            "services.bank_feed_rule_service.lookup_account_split_mapping_async",
            new=AsyncMock(
                return_value={
                    "split_account_number": "1120",
                    "split_account_name": "Undeposited Funds",
                }
            ),
        ):
            result = await classify_bank_transaction(txn, rules=[])

        self.assertEqual(result.source, "accounts_receivable_contact")
        self.assertEqual(result.account_number, "1100")
        self.assertFalse(result.requires_ai_review)

    async def test_bankcard_1292_quickbooks_rule_precedes_ar(self):
        txn = BankTransactionClassificationRequest(
            name="Garry Piltier",
            bank_text="BANKCARD-1292 DES:MTOT DEP",
            transaction_type="Deposit",
            amount=48,
        )
        rules = [
            rule(
                900,
                "BANKCARD-1292 subscription deposit",
                [condition(6, "BANKCARD-1292")],
                [
                    action(
                        0,
                        {
                            "account_number": "4500",
                            "account_name": "Subscription Services",
                        },
                    )
                ],
            )
        ]
        ar_lookup = patch(
            "services.bank_feed_rule_service.lookup_accounts_receivable_payment_contact",
            return_value={
                "account_number": "1100",
                "account_name": "Accounts Receivable",
                "display_name": "Garry Piltier",
            },
        )
        with ar_lookup as mocked_ar:
            result = await classify_bank_transaction(txn, rules=rules)

        self.assertEqual(result.source, "quickbooks_rule")
        self.assertEqual(result.account_number, "4500")
        mocked_ar.assert_not_called()

    async def test_ap_contact_precedes_one_to_one(self):
        txn = BankTransactionClassificationRequest(
            name="Quotemedia", transaction_type="Bill Payment (Check)", amount=187
        )
        with patch(
            "services.bank_feed_rule_service.lookup_accounts_payable_payment_contact",
            return_value={"account_number": "2000", "account_name": "Accounts Payable", "display_name": "Quotemedia"},
        ), patch(
            "services.bank_feed_rule_service.lookup_account_split_mapping_async",
            new=AsyncMock(return_value={"split_account_number": "6390", "split_account_name": "Subscriptions"}),
        ):
            result = await classify_bank_transaction(txn, rules=[])
        self.assertEqual(result.source, "accounts_payable_contact")
        self.assertEqual(result.account_number, "2000")


class AIProtectionTests(unittest.TestCase):
    def test_ai_cannot_override_quickbooks_rule(self):
        row = {
            "row_number": 1,
            "rule": "quickbooks_rule",
            "review_source": "quickbooks_rule",
            "target_field": "split_account",
            "ledger_account_number": "1000",
            "current_split_account_number": "1150",
            "current_target_account_number": "1150",
            "suggested_account_number": "6340",
            "requires_manual_review": False,
        }
        payload = {"suggestions": [row], "review_mode": "rules_then_ai"}
        ai = {
            "provider": "ai",
            "enabled": True,
            "suggestions": [{"row_number": 1, "target_field": "split_account", "suggested_account_number": "1940", "confidence": 1.0}],
        }
        merged = merge_ai_account_review_suggestions(payload, ai, include_all=True)
        actual = merged["suggestions"][0]
        self.assertEqual(actual["rule"], "quickbooks_rule")
        self.assertEqual(actual["suggested_account_number"], "6340")
        self.assertNotIn("ai_suggested_account_number", actual)


if __name__ == "__main__":
    unittest.main()
