from __future__ import annotations

import inspect

from app.provider_aware_ai_pipeline import ProviderAwareProductionAiPipeline


def test_recent_provider_context_uses_latest_revision_only_as_of_current_message() -> None:
    source = inspect.getsource(ProviderAwareProductionAiPipeline._recent_context_as_of)
    assert "mr.edited_at<=:as_of" in source
    assert "ORDER BY mr.revision_index DESC" in source
    assert "revision_index_as_of_message" in source


def test_future_deletion_cannot_remove_message_from_older_pit_context() -> None:
    source = inspect.getsource(ProviderAwareProductionAiPipeline._recent_context_as_of)
    assert "m.deleted_at IS NULL OR m.deleted_at>:as_of" in source
    assert "m.posted_at<=:as_of" in source


def test_current_message_lookup_does_not_fail_only_because_it_was_deleted_later() -> None:
    source = inspect.getsource(ProviderAwareProductionAiPipeline._source_context)
    current_lookup = source.split("if current is None", 1)[0]
    assert "deleted_at IS NULL" not in current_lookup
