from pathlib import Path


def test_render_start_does_not_mutate_telegram_board_on_deploy() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts" / "render-start.sh").read_text(encoding="utf-8")

    assert "telegram_live_board_reconcile" not in script
    assert "uvicorn app.main:app" in script
