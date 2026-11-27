"""Offline artifact generation, or explicitly consented development-only live seeding."""

import argparse
import asyncio
import json
from importlib.resources import files
from pathlib import Path

from app.config import Settings
from app.errors import ApplicationError, Forbidden
from app.security import Principal

DATA = Path(str(files("app").joinpath("data"))).resolve()


def consent(settings, confirmed):
    if not confirmed:
        raise Forbidden("Pass --confirm-azure-use before any Azure operation.")
    settings.require_live()
    if settings.app_env != "development" or settings.scope_id != "demo-payments-development":
        raise Forbidden("This command is restricted to the designated development scope.")


def operator(settings):
    # Script authority is server-owned and never accepted from command-line identity/scope.
    return Principal("development-cli", frozenset({"admin"}), frozenset({settings.scope_id}))


def generate_images():
    from PIL import Image, ImageDraw

    output = Path.cwd() / "data"
    output.mkdir(parents=True, exist_ok=True)

    def canvas(title, subtitle):
        image = Image.new("RGB", (1200, 720), "#f3f6fa")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1200, 100), fill="#14273e")
        draw.text((35, 25), title, fill="white", font_size=30)
        draw.text((35, 64), subtitle, fill="#d3dfeb", font_size=17)
        return image, draw

    image, draw = canvas(
        "Payments | Historical incident dashboard",
        "SYNTHETIC TRAINING EVIDENCE | 2026-10-19 14:10 UTC | Not live telemetry",
    )
    for index, (label, value) in enumerate(
        [("Error rate", "44.4%"), ("p95 latency", "1200 ms"), ("Pool saturation", "100%")]
    ):
        x = 40 + index * 390
        draw.rounded_rectangle((x, 140, x + 350, 280), radius=12, fill="white")
        draw.text((x + 20, 160), label, fill="#50637b", font_size=22)
        draw.text((x + 20, 210), value, fill="#a92530", font_size=36)
    draw.text(
        (40, 320),
        "Observed: 90 concurrent requests; pool limit 50; backend capacity 150.",
        fill="#182b40",
        font_size=23,
    )
    draw.text(
        (40, 370),
        "Restarts lowered latency briefly but did not change pool capacity.",
        fill="#182b40",
        font_size=22,
    )
    draw.text(
        (40, 440),
        "Verified historical correction: bounded pool adjustment 50 -> 100.",
        fill="#086bc1",
        font_size=25,
    )
    draw.text(
        (40, 510),
        "Valid deployment confirmed. Error rate returned to 0%; p95 to 80 ms.",
        fill="#182b40",
        font_size=22,
    )
    draw.text(
        (40, 630),
        "Source: synthetic incident INC-PAY-20261019 | Review before reuse",
        fill="#50637b",
        font_size=18,
    )
    image.save(output / "monitoring-dashboard.png")

    image, draw = canvas(
        "Payments | Connection pool utilization",
        "SYNTHETIC HISTORICAL CHART | Pool correction is not appropriate for deployment errors",
    )
    draw.line((100, 560, 1110, 560), fill="#50637b", width=2)
    draw.line((100, 150, 100, 560), fill="#50637b", width=2)
    for percentage in (0, 25, 50, 75, 100):
        y = 560 - percentage * 4
        draw.line((100, y, 1110, y), fill="#d9e1ea")
        draw.text((40, y - 10), f"{percentage}%", fill="#50637b", font_size=18)
    points = [
        (100 + i * 100, 560 - value * 4)
        for i, value in enumerate([70, 85, 100, 100, 100, 100, 90, 90, 90, 90, 90])
    ]
    draw.line(points, fill="#086bc1", width=5)
    draw.line((650, 160, 650, 560), fill="#a92530", width=3)
    draw.text((680, 300), "Approved adjustment", fill="#a92530", font_size=22)
    draw.text((680, 335), "pool 50 -> 100", fill="#a92530", font_size=22)
    draw.text(
        (100, 600),
        "14:00         14:05         14:10         14:15         14:20",
        fill="#50637b",
        font_size=20,
    )
    draw.text(
        (100, 660),
        "Historical: confirm fresh demand, validity and capacity before action.",
        fill="#182b40",
        font_size=20,
    )
    image.save(output / "pool-utilization.png")

    image, draw = canvas(
        "Payments | Development service architecture",
        "SYNTHETIC ARCHITECTURE EVIDENCE | Exact service target: payments",
    )
    boxes = [
        (40, 200, 300, 380, "Ingress", "90 requests"),
        (450, 200, 730, 380, "Payment workers", "Pool 50 / target 100"),
        (880, 200, 1160, 380, "Backend", "Capacity 150"),
    ]
    for x1, y1, x2, y2, title, detail in boxes:
        draw.rounded_rectangle(
            (x1, y1, x2, y2), radius=12, fill="white", outline="#086bc1", width=3
        )
        draw.text((x1 + 20, y1 + 35), title, fill="#182b40", font_size=25)
        draw.text((x1 + 20, y1 + 95), detail, fill="#50637b", font_size=19)
    for x1, x2 in ((300, 450), (730, 880)):
        draw.line((x1, 290, x2 - 10, 290), fill="#086bc1", width=4)
        draw.polygon([(x2 - 10, 280), (x2, 290), (x2 - 10, 300)], fill="#086bc1")
    draw.text(
        (70, 470),
        "Safety preconditions: valid deployment, fresh telemetry, demand <= 100,",
        fill="#182b40",
        font_size=24,
    )
    draw.text(
        (70, 515),
        "backend capacity >= demand, unchanged target/control versions.",
        fill="#182b40",
        font_size=24,
    )
    draw.text(
        (70, 610),
        "Invalid deployment configuration needs escalation, not pool expansion.",
        fill="#a92530",
        font_size=22,
    )
    image.save(output / "service-architecture.png")
    return ["monitoring-dashboard.png", "pool-utilization.png", "service-architecture.png"]


async def seed(settings, confirmed):
    consent(settings, confirmed)
    settings.require_live(
        "cosmos_endpoint", "embedding_endpoint", "blob_account_url", "blob_container"
    )
    from app.cosmos import CosmosRepository
    from app.evidence import EvidenceService
    from app.main import invoke
    from app.remediation import Governance
    from app.simulation import Simulation

    repo = await asyncio.to_thread(CosmosRepository, settings)
    evidence = EvidenceService(repo, settings)
    try:
        await invoke(Simulation(repo, settings).initialize)
        control = await invoke(Governance(repo, settings).control)
        if "_etag" not in control:
            try:
                await invoke(repo.create, control)
            except ApplicationError as error:
                if error.status_code != 409:
                    raise
        artifacts = json.loads((DATA / "artifacts.json").read_text(encoding="utf-8"))
        ids = []
        for artifact in artifacts:
            path = (DATA / artifact["file"]).resolve()
            suffix = ".png" if artifact["kind"] == "image" else ".md"
            if not path.is_relative_to(DATA) or path.suffix != suffix:
                raise Forbidden("Only designated synthetic artifacts may be seeded.")
            payload = {
                key: artifact[key]
                for key in ("kind", "title", "captured_at", "source")
                if key in artifact
            }
            payload["historical"] = True
            if artifact["kind"] == "image":
                payload.update(image_bytes=path.read_bytes(), content_type="image/png")
            else:
                payload["text"] = path.read_text(encoding="utf-8")
            result = await evidence.ingest(payload, operator(settings))
            ids.append({"id": result["id"], "cached": result.get("cached", False)})
        return {"status": "seeded", "artifacts": ids}
    finally:
        for service in (evidence, repo):
            close = getattr(service, "close", None)
            if close:
                await invoke(close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-azure-use", action="store_true")
    parser.add_argument(
        "--generate-images", action="store_true", help="Offline only; no Azure calls."
    )
    args = parser.parse_args()
    if args.generate_images:
        print(json.dumps({"generated": generate_images(), "azure_calls": 0}))
        return
    try:
        print(json.dumps(asyncio.run(seed(Settings(), args.confirm_azure_use))))
    except Exception:
        parser.exit(
            2, "Seed blocked or unavailable. Check consent, development scope and live setup.\n"
        )


if __name__ == "__main__":
    main()
