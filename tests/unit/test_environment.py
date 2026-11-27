import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import isolated_application_environment


@pytest.mark.parametrize(
    ("relative_module", "preserve"),
    [
        (Path("integration") / "test_live_azure.py", True),
        (Path("integration") / "test_other.py", False),
        (Path("unit") / "test_live_azure.py", False),
        (Path("live") / "test_other.py", False),
    ],
)
def test_only_canonical_live_module_preserves_explicit_environment(
    monkeypatch, relative_module, preserve
):
    values = {
        "ALLOW_AZURE_USE": "true",
        "COSMOS_ENDPOINT": "https://fixture.invalid",
        "SCOPE_ID": "test-fixture",
        "APP_ENV": "development",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    path = Path(__file__).resolve().parents[1] / relative_module
    request = SimpleNamespace(node=SimpleNamespace(path=path))
    isolated_application_environment.__wrapped__(monkeypatch, request)
    for name, value in values.items():
        assert os.environ.get(name) == (value if preserve else None)
