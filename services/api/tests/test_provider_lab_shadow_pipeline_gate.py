from uuid import uuid4

from app.production_ai_pipeline import ProductionAiMessagePipeline


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, statement, params=None):
        del params
        self.sql = str(statement)
        row = None
        if "s.status IN ('testing', 'shadow', 'live')" in self.sql:
            row = {
                "message_id": uuid4(),
                "raw_text": "XAUUSD BUY 3500 SL 3490 TP 3510",
                "raw_payload": {},
                "source_status": "shadow",
            }
        return _Result(row)


def test_production_pipeline_load_revision_accepts_shadow_sources() -> None:
    session = _Session()

    row = ProductionAiMessagePipeline._load_revision(
        session,
        source_id=uuid4(),
        telegram_message_id=12345,
        revision_index=0,
    )

    assert row is not None
    assert row["source_status"] == "shadow"
    assert "s.status IN ('testing', 'shadow', 'live')" in session.sql
