import pytest

from cba_bot.config import Settings
from cba_bot.runtime import Runtime
from cba_bot.tui import CBAApp


@pytest.mark.asyncio
async def test_tui_mounts_and_commands(tmp_path):
    corpus = tmp_path / "contracts"
    corpus.mkdir()
    (corpus / "stub.pdf").write_bytes(b"%PDF-")
    settings = Settings(
        provider="mock",
        data_dir=str(tmp_path / "data"),
        contracts_dir=str(corpus),
    )
    runtime = Runtime.build(settings)
    try:
        app = CBAApp(runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.settings.current_only is False
            await pilot.press("ctrl+l")
            await pilot.pause()
            app.query_one("#prompt").value = ""
            app._handle_command("/fresh")
            assert app.settings.current_only is True
            app._handle_command("/sector education")
            assert app.settings.sector == "education"
            app._handle_command("/state ny")
            assert app.settings.jurisdiction == "NY"
            app._handle_command("/fresh off")
            assert app.settings.current_only is False
    finally:
        runtime.close()
