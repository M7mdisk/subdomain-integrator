# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""subdomain-integrator ingress charm state component."""

import dataclasses
import hashlib
import re

import ops
from charms.traefik_k8s.v2.ingress import DataValidationError, IngressPerAppProvider

from .exception import CharmStateValidationBaseError


class IngressIntegrationDataValidationError(CharmStateValidationBaseError):
    """Exception raised when ingress relation data validation fails."""


def _dns_safe_name(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "app"


def _k8s_safe_name(*parts: str, limit: int = 63) -> str:
    name = "-".join(_dns_safe_name(part) for part in parts if part)
    if len(name) <= limit:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    trimmed = name[: limit - 9].rstrip("-")
    return f"{trimmed}-{digest}"


@dataclasses.dataclass(frozen=True)
class HTTPRouteResourceInformation:
    """A component of charm state containing route and service definitions."""

    relation_id: int
    application_name: str
    requirer_model_name: str
    service_name: str
    service_port: int
    service_port_name: str
    filters: list[dict]
    paths: list[str]
    hostname: str | None

    @classmethod
    def from_ingress_relation(
        cls,
        ingress_provider: IngressPerAppProvider,
        relation: ops.Relation,
        hostname: str,
    ) -> "HTTPRouteResourceInformation":
        """Populate fields from one ingress relation."""
        try:
            integration_data = ingress_provider.get_data(relation)
            application_name = integration_data.app.name
            model_name = integration_data.app.model
            service_port = integration_data.app.port
            service_name = _k8s_safe_name(
                ingress_provider.charm.app.name,
                model_name,
                application_name,
                f"r{relation.id}",
                "svc",
            )
            return cls(
                relation_id=relation.id,
                application_name=application_name,
                requirer_model_name=model_name,
                service_name=service_name,
                service_port=service_port,
                service_port_name=f"tcp-{service_port}",
                filters=[],
                paths=["/"],
                hostname=hostname,
            )
        except DataValidationError as exc:
            raise IngressIntegrationDataValidationError(
                "Validation of ingress relation data failed."
            ) from exc
