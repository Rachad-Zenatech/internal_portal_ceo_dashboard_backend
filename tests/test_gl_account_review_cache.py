import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from models.general_ledger_model import GeneralLedgerTransaction
from services.gl_account_suggestion_service import (
    _HYBRID_RETRIEVAL_CACHE,
    _build_hybrid_retrieval_candidates,
    _split_ai_review_payload_for_cache,
    build_gemini_account_review_suggestions,
)


class AccountReviewPayloadCacheTests(unittest.TestCase):
    def test_shared_payload_keeps_coa_out_of_chunk_payload(self):
        payload = {
            "task": "review",
            "rules": ["use the COA"],
            "chart_of_accounts": [{"account_number": "6100"}],
            "expected_output_schema": {"suggestions": []},
            "transactions": [{"row_number": 7}],
        }

        shared, chunk = _split_ai_review_payload_for_cache(payload)

        self.assertEqual(shared["chart_of_accounts"], payload["chart_of_accounts"])
        self.assertNotIn("transactions", shared)
        self.assertEqual(chunk["transactions"], [{"row_number": 7}])
        self.assertNotIn("chart_of_accounts", chunk)


class AccountReviewExplicitCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_coa_cache_is_created_once_and_reused_by_all_chunks(self):
        transactions = [
            GeneralLedgerTransaction(
                ledger_account_code="1000",
                ledger_account_name="Checking",
                ledger_account_type="Bank",
                split_account_code="6100",
                split_account_name="Office expense",
                amount=Decimal("10"),
                name=f"Vendor {index}",
            )
            for index in range(2)
        ]
        coa_rows = [
            {
                "account_number": "1000",
                "account_name": "Checking",
                "account_type": "Bank",
                "detail_type": "Checking",
            },
            {
                "account_number": "6100",
                "account_name": "Office expense",
                "account_type": "Expense",
                "detail_type": "Office expenses",
            },
        ]

        async def fake_ai_call(payload, model, **kwargs):
            row_number = payload["transactions"][0]["row_number"]
            self.assertNotIn("chart_of_accounts", payload)
            self.assertEqual(kwargs["cached_content_name"], "cachedContents/coa")
            return {
                "text": json.dumps(
                    {
                        "suggestions": [
                            {
                                "row_number": row_number,
                                "target_field": "none",
                                "suggested_account_number": None,
                                "suggested_account_name": None,
                                "confidence": 1,
                                "reason": "No change.",
                                "requires_manual_review": False,
                            }
                        ]
                    }
                ),
                "usage": {},
                "response_id": f"response-{row_number}",
                "retried_without_search": False,
                "cached_content_used": True,
            }

        with (
            patch(
                "services.gl_account_suggestion_service.AI_ACCOUNT_REVIEW_ROWS_PER_REQUEST",
                1,
            ),
            patch(
                "services.gl_account_suggestion_service._ai_review_model_candidates",
                return_value=["gemini-2.5-flash"],
            ),
            patch(
                "services.gl_account_suggestion_service._build_hybrid_retrieval_candidates",
                new=AsyncMock(
                    return_value=(
                        {},
                        {"enabled": False, "status": "disabled"},
                    )
                ),
            ),
            patch(
                "services.gl_account_suggestion_service._create_gemini_account_review_cache",
                new=AsyncMock(return_value=("cachedContents/coa", 2500)),
            ) as create_cache,
            patch(
                "services.gl_account_suggestion_service._delete_gemini_account_review_cache",
                new=AsyncMock(),
            ) as delete_cache,
            patch(
                "services.gl_account_suggestion_service._call_gemini_account_review",
                new=AsyncMock(side_effect=fake_ai_call),
            ) as ai_call,
        ):
            result = await build_gemini_account_review_suggestions(
                transactions,
                coa_rows,
                row_numbers=[1, 2],
                rows_per_request=1,
                concurrency_limit=2,
                include_xgboost=False,
                use_google_search=False,
            )

        create_cache.assert_awaited_once()
        delete_cache.assert_awaited_once_with("cachedContents/coa")
        self.assertEqual(ai_call.await_count, 2)
        self.assertEqual(result["completed_chunk_count"], 2)
        self.assertEqual(result["shared_context_cache"]["status"], "released")
        self.assertEqual(result["shared_context_cache"]["request_count"], 2)


class HybridRetrievalBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _HYBRID_RETRIEVAL_CACHE.clear()

    async def test_embeddings_are_batched_and_results_are_cached(self):
        duplicate = {
            "ledger_account_code": "1000",
            "ledger_account_name": "Checking",
            "ledger_account_type": "Bank",
            "split_account_code": "6100",
            "split_account_name": "Office expense",
            "amount": Decimal("10"),
            "name": "Same Vendor",
        }
        transactions = [
            GeneralLedgerTransaction(**duplicate),
            GeneralLedgerTransaction(**duplicate),
            GeneralLedgerTransaction(**{**duplicate, "name": "Other Vendor"}),
        ]

        async def embed_content(*, model, contents):
            return SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=[float(index), 1.0])
                    for index, _ in enumerate(contents)
                ]
            )

        gemini_client = MagicMock()
        gemini_client.aio.models.embed_content = AsyncMock(
            side_effect=embed_content
        )
        collection = MagicMock()

        def aggregate(_pipeline):
            cursor = MagicMock()
            cursor.to_list = AsyncMock(
                return_value=[
                    {
                        "account_number": "6100",
                        "account_name": "Office expense",
                        "account_type": "Expense",
                        "detail_type": "Office expenses",
                        "score": 0.9,
                    }
                ]
            )
            return cursor

        collection.aggregate.side_effect = aggregate
        mongo_client = MagicMock()
        mongo_client.admin.command = AsyncMock(return_value={"ok": 1})
        mongo_client.get_database.return_value.get_collection.return_value = collection

        with (
            patch.dict(
                "os.environ",
                {"MONGODB_URI": "mongodb://test", "GEMINI_API_KEY": "test"},
            ),
            patch(
                "motor.motor_asyncio.AsyncIOMotorClient",
                return_value=mongo_client,
            ),
            patch("google.genai.Client", return_value=gemini_client),
        ):
            first_candidates, first_status = (
                await _build_hybrid_retrieval_candidates(
                    transactions,
                    [1, 2, 3],
                )
            )
            second_candidates, second_status = (
                await _build_hybrid_retrieval_candidates(
                    transactions,
                    [1, 2, 3],
                )
            )

        self.assertEqual(gemini_client.aio.models.embed_content.await_count, 1)
        embedded_queries = (
            gemini_client.aio.models.embed_content.await_args.kwargs["contents"]
        )
        self.assertEqual(len(embedded_queries), 2)
        self.assertEqual(collection.aggregate.call_count, 2)
        self.assertEqual(sorted(first_candidates), [1, 2, 3])
        self.assertEqual(sorted(second_candidates), [1, 2, 3])
        self.assertEqual(first_status["cache_hit_count"], 0)
        self.assertEqual(second_status["cache_hit_count"], 3)


if __name__ == "__main__":
    unittest.main()
