# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""subdomain-integrator configuration."""

import logging
import typing

import ops
from charms.gateway_api_integrator.v0.gateway_route import valid_fqdn
from pydantic import BeforeValidator, Field, ValidationError
from pydantic.dataclasses import dataclass

from .exception import CharmStateValidationBaseError

TLS_CERTIFICATES_INTEGRATION = "certificates"

logger = logging.getLogger(__name__)


class InvalidCharmConfigError(CharmStateValidationBaseError):
    """Exception raised when a charm configuration is found to be invalid."""


@dataclass(frozen=True)
class CharmConfig:
    """A component of charm state that contains the charm's configuration."""

    base_domain: typing.Annotated[str, BeforeValidator(valid_fqdn)]
    gateway_class_name: str = Field(min_length=1)
    enforce_https: bool = Field()

    @classmethod
    def from_charm(
        cls,
        charm: ops.CharmBase,
        available_gateway_classes: list[str],
    ) -> "CharmConfig":
        """Create a CharmConfig class from a charm instance."""
        enforce_https = typing.cast(bool, charm.config.get("enforce-https", False))
        if charm.model.get_relation(TLS_CERTIFICATES_INTEGRATION) is None and enforce_https:
            raise InvalidCharmConfigError(
                "Certificates relation is needed if enforce-https is enabled."
            )

        gateway_class_name = typing.cast(str, charm.config.get("gateway-class"))
        if gateway_class_name not in available_gateway_classes:
            available_gateway_classes_str = ",".join(available_gateway_classes)
            logger.error(
                "Configured gateway class %s not present on the cluster. Available ones are: %r",
                gateway_class_name,
                available_gateway_classes_str,
            )
            raise InvalidCharmConfigError(
                f"Gateway class must be one of: [{available_gateway_classes_str}]"
            )

        base_domain = typing.cast(str | None, charm.config.get("base-domain"))
        if not base_domain:
            raise InvalidCharmConfigError("base-domain is required.")

        try:
            return cls(
                gateway_class_name=gateway_class_name,
                base_domain=base_domain,
                enforce_https=enforce_https,
            )
        except ValidationError as exc:
            raise InvalidCharmConfigError("invalid configuration") from exc
