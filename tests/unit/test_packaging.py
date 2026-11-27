import json
from importlib.resources import files
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from scripts.seed import DATA


def test_console_and_sample_resources_ignore_working_directory(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    with TestClient(create_app(Settings(_env_file=None, allow_azure_use=False))) as client:
        assert client.get("/").content == files("app").joinpath("web", "index.html").read_bytes()
        for name in ("app.js", "styles.css"):
            response = client.get("/static/" + name)
            assert response.status_code == 200
            assert response.content == files("app").joinpath("web", name).read_bytes()
    manifest = json.loads((DATA / "artifacts.json").read_text(encoding="utf-8"))
    assert len(manifest) == 6
    for artifact in manifest:
        assert (DATA / artifact["file"]).read_bytes()


def test_evaluation_output_uses_cwd_not_installed_assets(monkeypatch):
    from scripts import evaluate

    async def fake_evaluate(settings, confirmed):
        assert confirmed
        return {"records": []}

    root = Path(__file__).resolve().parents[2] / ".packaging-check" / "evaluation"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(evaluate, "evaluate", fake_evaluate)
    monkeypatch.setattr("sys.argv", ["incident-memory-evaluate", "--confirm-azure-use"])
    evaluate.main()
    output = root / "data" / "evaluation-results.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {"records": []}
    assert not output.is_relative_to(DATA)


def test_offline_image_generation_does_not_modify_installed_samples(monkeypatch):
    from scripts.seed import generate_images

    root = Path(__file__).resolve().parents[2] / ".packaging-check" / "images"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    images = (
        "monitoring-dashboard.png",
        "pool-utilization.png",
        "service-architecture.png",
    )
    originals = {name: (DATA / name).read_bytes() for name in images}
    assert generate_images() == list(images)
    for name in images:
        assert (root / "data" / name).read_bytes()
        assert (DATA / name).read_bytes() == originals[name]
