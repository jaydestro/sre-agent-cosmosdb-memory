import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from scripts.seed import consent

ROOT = Path(__file__).resolve().parents[2]


def test_delivered_console_has_accessible_primary_controls_and_no_inline_script():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/")
        assert response.status_code == 200
        for text in (
            'lang="en"',
            'aria-live="polite"',
            "Live service metrics",
            "Historical evidence",
            "Approval",
            "Exact remediation proposals",
            "Emergency stop",
            "Procedural memory",
            "Durable worker, elevation and action history",
            "Developer setup required",
        ):
            assert text in response.text
        assert '<script src="/static/app.js" defer></script>' in response.text
        assert "onclick=" not in response.text
        assert client.get("/static/styles.css").status_code == 200


def test_browser_code_only_uses_text_nodes_for_untrusted_content():
    source = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in source
    assert "textContent" in source
    assert "X-Requested-With" in source and "IncidentMemory" in source
    assert "URL.createObjectURL" in source
    assert "setInterval" in source and "10000" in source


def test_javascript_is_valid():
    completed = subprocess.run(
        ["node", "--check", str(ROOT / "app" / "web" / "app.js")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_synthetic_artifacts_are_actual_decodable_images_and_real_text():
    manifest = json.loads((ROOT / "app" / "data" / "artifacts.json").read_text(encoding="utf-8"))
    assert len([item for item in manifest if item["kind"] == "image"]) == 3
    for item in manifest:
        path = ROOT / "app" / "data" / item["file"]
        if item["kind"] == "image":
            with Image.open(path) as image:
                assert image.size == (1200, 720)
                assert image.format == "PNG"
                image.verify()
        else:
            text = path.read_text(encoding="utf-8")
            assert "Synthetic" in text
            assert len(text) > 500


def test_scripts_refuse_unconsented_live_writes_without_clients():
    import pytest

    from app.errors import Forbidden, Unavailable

    with pytest.raises(Forbidden):
        consent(Settings(_env_file=None, allow_azure_use=True), False)
    with pytest.raises(Unavailable):
        consent(Settings(_env_file=None, allow_azure_use=False), True)
    with pytest.raises(Forbidden):
        consent(
            Settings(
                _env_file=None,
                allow_azure_use=True,
                cosmos_endpoint="https://example.invalid",
                app_env="production",
            ),
            True,
        )


async def test_all_live_script_entrypoints_gate_before_any_clients(monkeypatch):
    import pytest

    from app.errors import Forbidden, Unavailable
    from scripts.evaluate import evaluate
    from scripts.reset import reset
    from scripts.seed import seed

    def forbidden_client(*args, **kwargs):
        raise AssertionError("The live client must never be constructed")

    monkeypatch.setattr("app.cosmos.CosmosRepository", forbidden_client)
    disabled = Settings(_env_file=None, allow_azure_use=False)
    enabled = disabled.model_copy(update={"allow_azure_use": True})
    for command, args in ((seed, ()), (reset, ("healthy",)), (evaluate, ())):
        with pytest.raises(Forbidden):
            await command(enabled, False, *args)
        with pytest.raises(Unavailable):
            await command(disabled, True, *args)
