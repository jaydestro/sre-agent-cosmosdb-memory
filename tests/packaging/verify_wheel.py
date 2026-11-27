"""Verify the built distribution from an isolated interpreter outside the checkout."""

import os
import re
import subprocess
import sys
import venv
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / ".packaging-check"
ASSETS = {
    "app/web/index.html",
    "app/web/app.js",
    "app/web/styles.css",
    "app/data/artifacts.json",
    "app/data/incident-report.md",
    "app/data/pool-runbook.md",
    "app/data/deployment-runbook.md",
    "app/data/monitoring-dashboard.png",
    "app/data/pool-utilization.png",
    "app/data/service-architecture.png",
}

PROBE = """
import json
import sys
from pathlib import Path
from importlib.resources import files
import app
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from scripts.seed import DATA
assert Path(app.__file__).is_relative_to(Path(sys.prefix)), app.__file__
manifest = json.loads((DATA / "artifacts.json").read_text(encoding="utf-8"))
assert len(manifest) == 6
for item in manifest:
    assert (DATA / item["file"]).read_bytes()
with TestClient(create_app(Settings(_env_file=None, allow_azure_use=False))) as client:
    html = client.get("/")
    assert html.status_code == 200
    assert html.content == files("app").joinpath("web", "index.html").read_bytes()
    for name in ("app.js", "styles.css"):
        response = client.get("/static/" + name)
        assert response.status_code == 200
        assert response.content == files("app").joinpath("web", name).read_bytes()
print("Installed wheel: HTML, static assets and all six sample fixtures verified.")
"""


def main():
    wheel = Path(sys.argv[1]).resolve()
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert ASSETS <= names, ASSETS - names
    for name in names:
        assert (
            name in ASSETS
            or name == "LICENSE"
            or (name.startswith(("app/", "scripts/")) and name.endswith(".py"))
            or ".dist-info/" in name
        ), f"Unexpected published file: {name}"
    venv.EnvBuilder(with_pip=True, clear=True).create(CHECK / "venv")
    bindir = CHECK / "venv" / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(ALLOW_AZURE_USE="false", LIVE_AZURE_TESTS="false", RUN_LIVE_TESTS="false")
    # The parent of the checkout contains no app package, demo assets, or configuration.
    cwd = ROOT.parent
    installed = subprocess.run(
        [str(python), "-I", "-m", "pip", "install", "--quiet", f"{wheel}[dev]"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
    )
    if installed.returncode:
        raise RuntimeError(re.sub(r"https?://\S+", "[package source]", installed.stderr))
    subprocess.run([str(python), "-I", "-c", PROBE], cwd=cwd, env=environment, check=True)
    for command in (
        "incident-memory",
        "incident-memory-seed",
        "incident-memory-reset",
        "incident-memory-evaluate",
    ):
        executable = bindir / (command + ".exe" if os.name == "nt" else command)
        result = subprocess.run(
            [str(executable), "--help"], cwd=cwd, env=environment, check=True, capture_output=True
        )
        assert b"usage:" in result.stdout
        print(f"{command} --help: verified outside checkout.")
    print(f"Publishable inventory verified: {len(names)} wheel entries; no private/meta assets.")


if __name__ == "__main__":
    main()
