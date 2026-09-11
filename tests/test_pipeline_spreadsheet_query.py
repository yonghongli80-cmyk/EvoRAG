from rag.pipeline import RAGPipeline
from rag.spreadsheets.coordinator import ComputedFact, SpreadsheetQueryOutcome
from rag.spreadsheets.query import StructuredCitation


class Coordinator:
    def query(self, question, *, kb_id, user):
        return SpreadsheetQueryOutcome(
            "structured_result", (ComputedFact("销售额 sum", "100.00"),),
            (StructuredCitation(7, "sheet_0001", "sheet_0001!c_0002:2-8"),), 7,
        )


def test_prepare_context_returns_exact_structured_route_without_retrieval():
    pipeline = object.__new__(RAGPipeline)
    pipeline._enterprise_mode = True
    pipeline._kb_id = "kb_sales"
    pipeline._user_db = object()
    pipeline._spreadsheet_coordinator = Coordinator()

    prepared, error = pipeline._prepare_context("销售额合计是多少", "s1", None, user={"id": 1}, kb_id="kb_sales")

    assert error is None
    assert prepared["route"] == "structured"
    assert prepared["answer"] == "销售额 sum：100.00"
    assert prepared["structured_result"]["rows_scanned"] == 7
