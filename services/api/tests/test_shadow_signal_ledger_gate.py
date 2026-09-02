from uuid import uuid4

from app.shadow_signal_ledger import ShadowAwareCanonicalSignalLedger


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
                "source_id": uuid4(),
                "provider_chat_id": -1001234567890,
                "provider_message_id": 12345,
                "source_posted_at": None,
                "original_text": "XAUUSD BUY 3500 SL 3490 TP 3510",
            }
        return _Result(row)


def test_shadow_aware_signal_ledger_accepts_shadow_sources() -> None:
    session = _Session()

    row = ShadowAwareCanonicalSignalLedger._message_revision_row(
        session,
        message_id=uuid4(),
        revision_index=0,
    )

    assert row is not None
    assert "s.status IN ('testing', 'shadow', 'live')" in session.sql
