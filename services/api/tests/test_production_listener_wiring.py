def test_production_main_uses_day38_database_execution_listener() -> None:
    import app.main as main

    assert main.build_day38_listener_manager.__module__ == "app.telegram_listener_day38"
    assert not hasattr(main, "build_day28_listener_manager")
