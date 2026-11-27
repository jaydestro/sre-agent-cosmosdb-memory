import os
from pathlib import Path

import pytest

from app.config import Settings
from tests.fakes import InMemoryRepository


@pytest.fixture(autouse=True)
def isolated_application_environment(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    live_module = Path(__file__).resolve().parent / "integration" / "test_live_azure.py"
    if request.node.path.resolve() == live_module:
        return
    names = {name.upper() for name in Settings.model_fields}
    for name in list(os.environ):
        if name.upper() in names:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def repo(settings: Settings) -> InMemoryRepository:
    return InMemoryRepository(settings.scope_id)
