import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from models.general_ledger_model import GeneralLedgerTransaction
from services import account_split_mapping_service as mapping_service
from tools.gl_imports import _empty_current_target_row_numbers


class HistoryMappingThresholdTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "split_count": 1,
            "split_signature": "6340 Office Supplies",
            "occurrence_count": mapping_service.MIN_OCCURRENCES_FOR_PROMOTION,
            "source_file_count": mapping_service.MIN_SAVED_IMPORTS_FOR_PROMOTION,
            "source_month_count": mapping_service.MIN_ACCOUNTING_MONTHS_FOR_PROMOTION,
        }
        row.update(overrides)
        return row

    def test_mapping_requires_all_independent_history_thresholds(self):
        self.assertTrue(
            mapping_service._history_mapping_is_promotable(self._row())
        )
        self.assertFalse(
            mapping_service._history_mapping_is_promotable(
                self._row(
                    occurrence_count=(
                        mapping_service.MIN_OCCURRENCES_FOR_PROMOTION - 1
                    )
                )
            )
        )
        self.assertFalse(
            mapping_service._history_mapping_is_promotable(
                self._row(
                    source_file_count=(
                        mapping_service.MIN_SAVED_IMPORTS_FOR_PROMOTION - 1
                    )
                )
            )
        )
        self.assertFalse(
            mapping_service._history_mapping_is_promotable(
                self._row(
                    source_month_count=(
                        mapping_service.MIN_ACCOUNTING_MONTHS_FOR_PROMOTION - 1
                    )
                )
            )
        )
        self.assertFalse(
            mapping_service._history_mapping_is_promotable(
                self._row(split_count=2)
            )
        )

    def test_curated_workbook_rows_are_seeded_as_inactive_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed_path = Path(temp_dir) / "mappings.json"
            seed_path.write_text(
                json.dumps(
                    [
                        {
                            "account_name": "Example Vendor",
                            "split": "6340 Office Supplies",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with patch.object(mapping_service, "SEED_FILE", seed_path):
                records = mapping_service._seed_records()

        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["is_active"])


class HistoryMappingQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_uses_bank_side_and_independent_import_counts(self):
        conn = AsyncMock()
        conn.fetch.return_value = []

        await mapping_service._fetch_history_stats(conn, ["example vendor"])

        sql = conn.fetch.await_args.args[0]
        self.assertIn("l.is_bank_line = TRUE", sql)
        self.assertIn("COUNT(DISTINCT source_file_id)", sql)
        self.assertIn("COUNT(DISTINCT accounting_month)", sql)
        self.assertIn("NOT IN ('bank', 'creditcard')", sql)


class SaveTargetScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_bank_rows_do_not_require_a_save_target(self):
        bank_row = GeneralLedgerTransaction(
            ledger_account_code="1000",
            ledger_account_name="Checking",
            ledger_account_type="Bank",
            split_account_code=None,
            amount=Decimal("-10"),
        )
        non_bank_row = GeneralLedgerTransaction(
            ledger_account_code="6340",
            ledger_account_name="Office Supplies",
            ledger_account_type="Expense",
            split_account_code=None,
            amount=Decimal("10"),
        )

        with (
            patch(
                "tools.gl_imports.get_all_chart_of_accounts",
                AsyncMock(return_value=[]),
            ),
            patch(
                "tools.gl_imports.scope_account_review_transactions",
                return_value=([bank_row], [1], 1),
            ),
        ):
            missing = await _empty_current_target_row_numbers(
                [bank_row, non_bank_row]
            )

        self.assertEqual(missing, [1])


if __name__ == "__main__":
    unittest.main()
