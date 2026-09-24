from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0072_provider_profile_dossier.py"


def test_dossier_exposes_each_required_provider_learning_dimension() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for field in (
        "provider_profile_gate_status",
        "gate_state",
        "blockers",
        "observed_messages",
        "active_days",
        "session_mix",
        "accepted_signals",
        "entry_style",
        "order_style",
        "direction_style",
        "median_stop_distance",
        "median_tp_count",
        "runner_rate",
        "management_style",
        "management_event_mix",
        "message_format",
        "message_sequence",
        "provider_vocabulary",
        "masked_grammar_examples",
        "behaviour_signature_sha256",
    ):
        assert field in source
