import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jobs.handlers import parse_gl_dry_run_preview_handler
from models.general_ledger_model import GeneralLedgerTransaction
from services.gl_dry_run_import_service import build_dry_run_preview_from_path


class EarlyAccountReviewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_callback_runs_before_preview_is_built(self):
        events = []
        transaction = GeneralLedgerTransaction(
            ledger_account_code="1000",
            ledger_account_name="Checking",
            ledger_account_type="Bank",
            split_account_code="6100",
            split_account_name="Office expense",
            amount=Decimal("10"),
        )

        async def callback(cache_info):
            events.append("review_queued")
            self.assertEqual(cache_info["preview_token"], "preview-token")
            self.assertEqual(cache_info["summary"]["bank_lines"], 1)
            return {"status": "queued", "job_id": "42"}

        async def build_preview(**kwargs):
            events.append("preview_built")
            return {"pagination": {"total_rows": len(kwargs["transactions"])}}

        with (
            patch(
                "services.gl_dry_run_import_service.get_company_book_for_import",
                AsyncMock(
                    return_value={
                        "company_id": 7,
                        "company_book_id": 8,
                        "company_name": "Test Company",
                        "format_code": "qb_desktop",
                        "format_name": "QuickBooks Desktop",
                    }
                ),
            ),
            patch.dict(
                "services.gl_dry_run_import_service.PARSERS",
                {"qb_desktop": lambda _path: [transaction]},
            ),
            patch(
                "services.gl_dry_run_import_service.validate_and_enrich_against_db_coa",
                AsyncMock(return_value={}),
            ),
            patch(
                "services.gl_dry_run_import_service.create_dry_run_preview_cache",
                return_value={"token": "preview-token", "expires_at": "later"},
            ),
            patch(
                "services.gl_dry_run_import_service.build_import_preview_from_transactions",
                build_preview,
            ),
        ):
            result = await build_dry_run_preview_from_path(
                file_path="test.xlsx",
                source_filename="test.xlsx",
                content_type=None,
                company_book_id=8,
                preview_limit=None,
                preview_cache_ready_callback=callback,
            )

        self.assertEqual(events, ["review_queued", "preview_built"])
        self.assertEqual(
            result["early_account_review"],
            {"status": "queued", "job_id": "42"},
        )


class EarlyAccountReviewHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_handler_queues_ai_as_soon_as_cache_is_ready(self):
        queued_payloads = []

        async def fake_create_job(job_data):
            queued_payloads.append(job_data.input)
            return SimpleNamespace(id=91, status="queued_local")

        async def fake_build(**kwargs):
            early_review = await kwargs["preview_cache_ready_callback"](
                {
                    "preview_token": "preview-token",
                    "summary": {
                        "company_id": 7,
                        "company_name": "Test Company",
                    },
                    "metadata": {"format_code": "qb_desktop"},
                }
            )
            return {
                "summary": {"dry_run_preview_token": "preview-token"},
                "preview": {"rows": []},
                "dry_run_preview_token": "preview-token",
                "early_account_review": early_review,
            }

        with (
            patch("jobs.handlers.execute", AsyncMock(return_value={"status": "processing"})),
            patch(
                "jobs.handlers.read_dry_run_preview_upload",
                AsyncMock(return_value=b"test workbook"),
            ),
            patch("jobs.handlers.create_background_job", fake_create_job),
            patch("jobs.handlers.build_dry_run_preview_from_path", fake_build),
        ):
            result = await parse_gl_dry_run_preview_handler(
                {
                    "id": 12,
                    "user_id": 3,
                    "input": {
                        "filePath": "test.xlsx",
                        "fileName": "test.xlsx",
                        "companyBookId": 8,
                    },
                }
            )

        self.assertNotIn("preview", result)
        self.assertEqual(result["early_account_review"]["job_id"], "91")
        self.assertEqual(len(queued_payloads), 1)
        self.assertEqual(queued_payloads[0]["previewToken"], "preview-token")
        self.assertTrue(queued_payloads[0]["useAi"])
        self.assertEqual(queued_payloads[0]["aiRowsPerRequest"], 100)
        self.assertEqual(queued_payloads[0]["aiConcurrencyLimit"], 5)
        self.assertFalse(queued_payloads[0]["aiUseGoogleSearch"])
        self.assertEqual(queued_payloads[0]["startedFromUploadJobId"], "12")


if __name__ == "__main__":
    unittest.main()
