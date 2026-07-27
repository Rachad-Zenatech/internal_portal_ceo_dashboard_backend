import unittest
from decimal import Decimal
from unittest.mock import patch

from models.general_ledger_model import GeneralLedgerTransaction
from services.gl_account_suggestion_service import (
    ACCOUNT_SUGGESTION_DECISION_POLICY,
    ACCOUNT_SUGGESTION_PIPELINE_VERSION,
    AccountSuggestion,
    _apply_account_suggestion_review_markers,
    suggest_split_account,
)
from services.gl_persistence_service import _account_review_decision_outcome


def _transaction(**overrides):
    values = {
        "ledger_account_code": "1000",
        "ledger_account_name": "Checking",
        "ledger_account_type": "Bank",
        "amount": Decimal("-100"),
        "name": "Example vendor",
        "memo": "Example transaction",
    }
    values.update(overrides)
    return GeneralLedgerTransaction(**values)


def _suggestion(account_number: str, rule: str) -> AccountSuggestion:
    return AccountSuggestion(
        suggested_account_number=account_number,
        suggested_account_name=f"Account {account_number}",
        confidence=1.0,
        reason=f"Matched {rule}.",
        rule=rule,
    )


class AccountSuggestionPipelinePrecedenceTests(unittest.TestCase):
    def test_transfer_structure_beats_company_rule(self):
        txn = _transaction(
            transaction_type="Transfer",
            memo="Online Banking transfer Confirmation# 12345",
        )
        with patch(
            "services.gl_account_suggestion_service._quickbooks_rule_suggestion",
            return_value=_suggestion("6710", "quickbooks_rule"),
        ) as quickbooks:
            result = suggest_split_account(
                txn,
                [
                    {
                        "account_number": "1000",
                        "account_name": "Checking",
                        "account_type": "Bank",
                    }
                ],
                use_xgboost=False,
                transfer_counterparty={
                    "account_number": "1035",
                    "account_name": "Other checking",
                },
            )

        self.assertEqual(result.rule, "bank_transfer_paired")
        self.assertEqual(result.suggested_account_number, "1035")
        quickbooks.assert_not_called()

    def test_company_rule_beats_contacts_and_history(self):
        txn = _transaction()
        with (
            patch(
                "services.gl_account_suggestion_service._quickbooks_rule_suggestion",
                return_value=_suggestion("6710", "quickbooks_rule"),
            ),
            patch(
                "services.gl_account_suggestion_service._accounts_receivable_contact_suggestion",
                return_value=_suggestion("1100", "accounts_receivable_contact"),
            ) as receivable,
            patch(
                "services.gl_account_suggestion_service._account_split_lookup_suggestion",
                return_value=_suggestion("6340", "account_split_lookup"),
            ) as history,
        ):
            result = suggest_split_account(txn, [], use_xgboost=False)

        self.assertEqual(result.rule, "quickbooks_rule")
        receivable.assert_not_called()
        history.assert_not_called()

    def test_approved_history_beats_semantic_inference(self):
        txn = _transaction()
        with (
            patch(
                "services.gl_account_suggestion_service._quickbooks_rule_suggestion",
                return_value=None,
            ),
            patch(
                "services.gl_account_suggestion_service._accounts_receivable_contact_suggestion",
                return_value=None,
            ),
            patch(
                "services.gl_account_suggestion_service._accounts_payable_contact_suggestion",
                return_value=None,
            ),
            patch(
                "services.gl_account_suggestion_service._account_split_lookup_suggestion",
                return_value=_suggestion("6340", "account_split_lookup"),
            ),
            patch(
                "services.gl_account_suggestion_service._evidence_semantic_account_suggestion",
                return_value=_suggestion("6360", "coa_semantic_match"),
            ) as semantic,
        ):
            result = suggest_split_account(txn, [], use_xgboost=False)

        self.assertEqual(result.rule, "account_split_lookup")
        semantic.assert_not_called()


class AccountSuggestionOutcomeTests(unittest.TestCase):
    def test_explicit_outcomes_and_provenance(self):
        cases = (
            (
                {
                    "rule": "keep_current",
                    "suggested_account_number": "6100",
                    "current_target_account_number": "6100",
                    "requires_manual_review": False,
                },
                "keep_current",
            ),
            (
                {
                    "rule": "quickbooks_rule",
                    "suggested_account_number": "6200",
                    "current_target_account_number": "6100",
                    "requires_manual_review": False,
                },
                "suggested_change",
            ),
            (
                {
                    "rule": "ai_review_needs_manual",
                    "suggested_account_number": "6300",
                    "current_target_account_number": None,
                    "requires_manual_review": True,
                },
                "manual_review",
            ),
            (
                {
                    "rule": "unknown_current_split",
                    "suggested_account_number": None,
                    "current_target_account_number": None,
                    "requires_manual_review": False,
                },
                "no_suggestion",
            ),
        )

        for values, expected in cases:
            with self.subTest(expected=expected):
                row = _apply_account_suggestion_review_markers(dict(values))
                self.assertEqual(row["decision_outcome"], expected)
                self.assertEqual(
                    row["decision_policy"],
                    ACCOUNT_SUGGESTION_DECISION_POLICY,
                )
                self.assertEqual(
                    row["pipeline_version"],
                    ACCOUNT_SUGGESTION_PIPELINE_VERSION,
                )
                self.assertIsInstance(row["decision_priority"], int)

    def test_non_bank_preview_is_not_applicable(self):
        self.assertEqual(
            _account_review_decision_outcome(
                {
                    "is_bank_transaction": False,
                    "requires_ai_review": False,
                    "requires_human_review": False,
                }
            ),
            "not_applicable",
        )


if __name__ == "__main__":
    unittest.main()
