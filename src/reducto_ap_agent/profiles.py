"""Deployment profiles: cloud and VPC.

The profile is the blueprint's reason to exist, so it is code,
not prose: :func:`resolve_profile` turns ``DEPLOYMENT_PROFILE``
(plus explicit overrides) into the concrete wiring the adapters
use, and the tests pin both profiles' endpoints.

- ``cloud`` — the document engine is Reducto's hosted API at
  ``https://platform.reducto.ai``. Document bytes leave the
  customer's boundary to Reducto's cloud, under Reducto's own
  enterprise terms.
- ``vpc`` — the document engine is reached at an internal
  endpoint inside the customer's boundary (``REDUCTO_BASE_URL``,
  default ``http://reducto-engine:8080`` — the compose service
  name in ``docker-compose.vpc.yml``). In this profile the
  blueprint's topology keeps the engine internal-only: the
  compose overlay publishes no port for it, and document traffic
  never crosses the boundary. The profile wires the port to the
  configured internal endpoint — it does not conjure a Reducto
  binary; a licensed self-hosted engine, or the bundled
  labelled stand-in service used by the demo stack, answers at
  that endpoint speaking the same API.

The difference between the profiles is endpoint + topology,
nothing else: the same adapter, the same pipeline, the same
guardrails run in both.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import deployment_profile, env_str

CLOUD_BASE_URL = "https://platform.reducto.ai"
VPC_DEFAULT_BASE_URL = "http://reducto-engine:8080"


@dataclass(frozen=True)
class DeploymentProfile:
    name: str  # "cloud" | "vpc"
    reducto_base_url: str
    engine_internal_only: bool  # topology claim the compose file keeps

    @property
    def is_vpc(self) -> bool:
        return self.name == "vpc"


def resolve_profile() -> DeploymentProfile:
    name = deployment_profile()
    override = env_str("REDUCTO_BASE_URL")
    if name == "vpc":
        return DeploymentProfile(
            name="vpc",
            reducto_base_url=override or VPC_DEFAULT_BASE_URL,
            engine_internal_only=True,
        )
    if name == "cloud":
        return DeploymentProfile(
            name="cloud",
            reducto_base_url=override or CLOUD_BASE_URL,
            engine_internal_only=False,
        )
    raise ValueError(
        f"unknown DEPLOYMENT_PROFILE {name!r} — expected 'cloud' or 'vpc'"
    )
