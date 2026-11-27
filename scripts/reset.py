"""Reset only the designated development service; retain durable action history."""

import argparse
import asyncio
import json

from app.config import Settings
from app.errors import Forbidden
from scripts.seed import consent, operator


async def reset(settings, confirmed, scenario):
    consent(settings, confirmed)
    if not settings.allow_demo_reset:
        raise Forbidden("Development reset must be explicitly enabled.")
    from app.cosmos import CosmosRepository
    from app.main import invoke
    from app.remediation import Governance
    from app.simulation import Simulation

    repo = await asyncio.to_thread(CosmosRepository, settings)
    try:
        governance = Governance(repo, settings)
        control = await invoke(governance.control)
        # A fresh paused control version invalidates all previously bound approvals/policies.
        await invoke(governance.set_mode, "paused", operator(settings), control["version"])
        snapshot = await invoke(Simulation(repo, settings).inject, scenario, operator(settings))
        return {
            "status": "reset",
            "scenario": scenario,
            "service_version": snapshot["version"],
            "mode": "paused",
            "history_retained": True,
        }
    finally:
        await invoke(repo.close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-azure-use", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=["pool_exhaustion", "deployment_error", "healthy"],
        default="pool_exhaustion",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(reset(Settings(), args.confirm_azure_use, args.scenario))))
    except Exception:
        parser.exit(
            2, "Reset blocked or unavailable. Check consent and designated development setup.\n"
        )


if __name__ == "__main__":
    main()
