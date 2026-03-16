#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""subdomain-integrator charm file."""

import logging
import re
import typing
import uuid
from ipaddress import ip_address

from charmlibs.interfaces.tls_certificates import (
    CertificateAvailableEvent,
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesRequiresV4,
)
from charms.bind.v0.dns_record import (
    DNSRecordRequirerData,
    DNSRecordRequires,
    RecordClass,
    RecordType,
    RequirerEntry,
)
from charms.haproxy.v2.haproxy_route import HaproxyRouteRequirer
from charms.traefik_k8s.v2.ingress import (
    IngressPerAppDataProvidedEvent,
    IngressPerAppDataRemovedEvent,
    IngressPerAppProvider,
)
from lightkube import Client
from lightkube.core.client import LabelSelector
from lightkube.generic_resource import create_global_resource
from ops import BlockedStatus
from ops.charm import ActionEvent, CharmBase, RelationCreatedEvent, RelationJoinedEvent
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, Relation, WaitingStatus

from client import get_client
from resource_manager.gateway import GatewayResourceDefinition, GatewayResourceManager
from resource_manager.http_route import (
    HTTPRouteResourceDefinition,
    HTTPRouteResourceManager,
    HTTPRouteType,
)
from resource_manager.permission import map_k8s_auth_exception
from resource_manager.secret import SecretResourceDefinition, TLSSecretResourceManager
from resource_manager.service import ServiceResourceDefinition, ServiceResourceManager
from state.config import CharmConfig
from state.gateway import GatewayResourceInformation
from state.http_route import HTTPRouteResourceInformation, IngressIntegrationDataValidationError
from state.tls import TLSInformation
from state.validation import validate_config_and_integration

logger = logging.getLogger(__name__)
CREATED_BY_LABEL = "subdomain-integrator.charm.juju.is/managed-by"
INGRESS_RELATION = "ingress"
HAPROXY_ROUTE_RELATION = "haproxy-route"
TLS_CERT_RELATION = "certificates"
UUID_NAMESPACE = uuid.UUID("f8f206da-a7f8-4206-b044-30be3724a09d")
CUSTOM_RESOURCE_GROUP_NAME = "gateway.networking.k8s.io"
GATEWAY_CLASS_RESOURCE_NAME = "GatewayClass"
GATEWAY_CLASS_PLURAL = "gatewayclasses"


def normalize_dns_label(value: str) -> str:
    """Normalize application name into a DNS-label-safe string."""
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "app"


class SubdomainIntegratorCharm(CharmBase):
    """Main charm class for subdomain-integrator."""

    def __init__(self, *args) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args)

        self._ingress_provider = IngressPerAppProvider(charm=self, relation_name=INGRESS_RELATION)
        self._haproxy_route = HaproxyRouteRequirer(self, HAPROXY_ROUTE_RELATION)
        self.dns_record_requirer = DNSRecordRequires(self)

        self.certificates = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=TLS_CERT_RELATION,
            certificate_requests=self._get_certificate_requests(),
            mode=Mode.APP,
            refresh_events=[
                self.on.config_changed,
                self._ingress_provider.on.data_provided,
                self._ingress_provider.on.data_removed,
            ],
        )

        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.certificates_relation_joined, self._on_certificates_relation_joined)
        self.framework.observe(self.on.certificates_relation_broken, self._on_certificates_relation_broken)
        self.framework.observe(self.certificates.on.certificate_available, self._on_certificate_available)
        self.framework.observe(self.on.get_certificate_action, self._on_get_certificate_action)
        self.framework.observe(self._ingress_provider.on.data_provided, self._on_data_provided)
        self.framework.observe(self._ingress_provider.on.data_removed, self._on_data_removed)
        self.framework.observe(self.on[HAPROXY_ROUTE_RELATION].relation_created, self._on_data_provided)
        self.framework.observe(self.on[HAPROXY_ROUTE_RELATION].relation_changed, self._on_data_provided)
        self.framework.observe(self.on[HAPROXY_ROUTE_RELATION].relation_broken, self._on_data_provided)
        self.framework.observe(self.on.dns_record_relation_created, self._on_dns_record_relation_created)
        self.framework.observe(self.on.dns_record_relation_joined, self._on_dns_record_relation_joined)

    def _get_certificate_requests(self) -> list[CertificateRequestAttributes]:
        base_domain = typing.cast(str | None, self.config.get("base-domain"))
        if base_domain:
            return [CertificateRequestAttributes(common_name=base_domain)]
        return []

    @property
    def _labels(self) -> LabelSelector:
        return {CREATED_BY_LABEL: self.app.name}

    @validate_config_and_integration(defer=False)
    def _on_config_changed(self, _: typing.Any) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_certificates_relation_joined(self, _: typing.Any) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_certificates_relation_broken(self, _: typing.Any) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_start(self, _: typing.Any) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_get_certificate_action(self, event: ActionEvent) -> None:
        if not self.model.get_relation(TLS_CERT_RELATION):
            event.fail("Certificate relation not ready.")
            return

        hostname = event.params["hostname"]
        provider_certificates = self.certificates.get_provider_certificates()
        for certificate in provider_certificates:
            if certificate.certificate.common_name == hostname:
                event.set_results(
                    {
                        "certificate": str(certificate.certificate),
                        "ca": str(certificate.ca),
                        "chain": "\n\n".join(str(cert) for cert in certificate.chain),
                    }
                )
                return
        event.fail(f"Missing or incomplete certificate data for {hostname}")

    @validate_config_and_integration(defer=False)
    def _on_certificate_available(self, _: CertificateAvailableEvent) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_data_provided(self, _: IngressPerAppDataProvidedEvent) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_data_removed(self, _: IngressPerAppDataRemovedEvent) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_dns_record_relation_created(self, _: RelationCreatedEvent) -> None:
        self._reconcile()

    @validate_config_and_integration(defer=False)
    def _on_dns_record_relation_joined(self, _: RelationJoinedEvent) -> None:
        self._reconcile()

    def _reconcile(self) -> None:
        if not self.unit.is_leader():
            self.unit.status = BlockedStatus("Deploying more than one unit is not supported.")
            return

        self.unit.status = MaintenanceStatus("Creating resources.")
        client = get_client(field_manager=self.app.name, namespace=self.model.name)
        config = CharmConfig.from_charm(self, self.available_gateway_classes())

        tls_information = None
        if config.enforce_https:
            tls_information = TLSInformation.from_charm(self, config.base_domain, self.certificates)

        self._define_secret_resources(client, tls_information)

        gateway_resource_information = GatewayResourceInformation.from_charm(self)
        gateway_resource_manager = GatewayResourceManager(labels=self._labels, client=client)
        self._define_gateway_resource(
            gateway_resource_manager, gateway_resource_information, config, tls_information
        )
        self._define_ingress_resources_and_publish_urls(
            client,
            config,
            tls_information,
            gateway_resource_information,
            gateway_resource_manager,
        )

        self._update_dns_record_relation(
            gateway_resource_manager,
            config.base_domain,
            gateway_resource_information,
        )
        self._provide_haproxy_route_requirements(
            gateway_resource_manager,
            gateway_resource_information,
            config,
        )
        self._set_status_gateway_address(gateway_resource_manager, gateway_resource_information)

    def _update_dns_record_relation(
        self,
        resource_manager: GatewayResourceManager,
        base_domain: str,
        gateway_resource_information: GatewayResourceInformation,
    ) -> None:
        relation = self.model.get_relation(self.dns_record_requirer.relation_name)
        if not relation:
            return
        if not resource_manager.current_gateway_resource():
            return

        gateway_address = resource_manager.gateway_address(gateway_resource_information.gateway_name)
        if not gateway_address:
            return

        entry = RequirerEntry(
            domain=base_domain,
            host_label="@",
            ttl=600,
            record_class=RecordClass.IN,
            record_type=RecordType.A,
            record_data=ip_address(gateway_address),
            uuid=uuid.uuid5(UUID_NAMESPACE, str(base_domain) + " " + str(gateway_address)),
        )
        dns_record_requirer_data = DNSRecordRequirerData(dns_entries=[entry])
        self.dns_record_requirer.update_relation_data(relation, dns_record_requirer_data)

    def _define_gateway_resource(
        self,
        resource_manager: GatewayResourceManager,
        gateway_resource_information: GatewayResourceInformation,
        config: CharmConfig,
        tls_information: TLSInformation | None,
    ) -> None:
        resource_definition = GatewayResourceDefinition(
            gateway_resource_information,
            config,
            tls_information,
        )
        gateway = resource_manager.define_resource(resource_definition)
        resource_manager.cleanup_resources(exclude=[gateway])

    def _define_secret_resources(self, client: Client, tls_information: TLSInformation | None) -> None:
        if tls_information is None:
            resource_manager = TLSSecretResourceManager(labels=self._labels, client=client)
            resource_manager.cleanup_resources(exclude=[])
            return

        resource_definition = SecretResourceDefinition.from_tls_information(tls_information)
        resource_manager = TLSSecretResourceManager(labels=self._labels, client=client)
        secret = resource_manager.define_resource(resource_definition)
        resource_manager.cleanup_resources(exclude=[secret])

    def _define_ingress_resources_and_publish_urls(
        self,
        client: Client,
        config: CharmConfig,
        tls_information: TLSInformation | None,
        gateway_resource_information: GatewayResourceInformation,
        gateway_resource_manager: GatewayResourceManager,
    ) -> None:
        service_resource_manager = ServiceResourceManager(self._labels, client)
        http_route_resource_manager = HTTPRouteResourceManager(self._labels, client)

        managed_services = []
        managed_http_routes = []
        gateway_address = gateway_resource_manager.gateway_address(gateway_resource_information.gateway_name)

        for relation in self._ingress_provider.relations:
            try:
                hostname = self._relation_hostname(relation, config.base_domain)
                info = HTTPRouteResourceInformation.from_ingress_relation(
                    self._ingress_provider,
                    relation,
                    hostname,
                )
            except IngressIntegrationDataValidationError:
                logger.exception("Invalid ingress relation data for relation id %s", relation.id)
                continue

            route_defs = [
                HTTPRouteResourceDefinition(
                    info,
                    gateway_resource_information,
                    HTTPRouteType.HTTP,
                    redirect_https=config.enforce_https,
                )
            ]
            if tls_information is not None:
                route_defs.append(
                    HTTPRouteResourceDefinition(
                        info,
                        gateway_resource_information,
                        HTTPRouteType.HTTPS,
                    )
                )

            for route_def in route_defs:
                managed_http_routes.append(http_route_resource_manager.define_resource(route_def))

            managed_services.append(
                service_resource_manager.define_resource(ServiceResourceDefinition(info))
            )

            scheme = "https" if config.enforce_https else "http"
            if gateway_address:
                self._ingress_provider.publish_url(relation, f"{scheme}://{hostname}")

        http_route_resource_manager.cleanup_resources(exclude=managed_http_routes)
        service_resource_manager.cleanup_resources(exclude=managed_services)

    def _provide_haproxy_route_requirements(
        self,
        resource_manager: GatewayResourceManager,
        gateway_resource_information: GatewayResourceInformation,
        config: CharmConfig,
    ) -> None:
        """Expose gateway address through haproxy-route when related."""
        if self.model.get_relation(HAPROXY_ROUTE_RELATION) is None:
            return
        self._haproxy_route.relation = self.model.get_relation(HAPROXY_ROUTE_RELATION)

        gateway_addresses = resource_manager.gateway_address(
            gateway_resource_information.gateway_name
        )
        if not gateway_addresses:
            logger.warning("Gateway address unavailable, skipping haproxy-route update.")
            return

        hosts = [addr.strip() for addr in gateway_addresses.split(",") if addr.strip()]
        if not hosts:
            return

        relation_hostnames = []
        for relation in self._ingress_provider.relations:
            try:
                relation_hostnames.append(self._relation_hostname(relation, config.base_domain))
            except IngressIntegrationDataValidationError:
                logger.exception("Invalid ingress relation data for relation id %s", relation.id)

        primary_hostname = relation_hostnames[0] if relation_hostnames else f"*.{config.base_domain}"
        additional_hostnames = relation_hostnames[1:] if len(relation_hostnames) > 1 else []

        # HAProxy forwards wildcard subdomains to Gateway API, which performs per-app hostname routing.
        self._haproxy_route.provide_haproxy_route_requirements(
            service=self.app.name,
            ports=[80],
            protocol="http",
            hosts=hosts,
            hostname=primary_hostname,
            additional_hostnames=additional_hostnames,
            paths=["/"],
            allow_http=False,
        )

    def _relation_hostname(self, relation: Relation, base_domain: str) -> str:
        relation_data = self._ingress_provider.get_data(relation)
        app_name = normalize_dns_label(relation_data.app.name)
        return f"{app_name}.{base_domain}"

    def _set_status_gateway_address(
        self,
        resource_manager: GatewayResourceManager,
        gateway_resource_information: GatewayResourceInformation,
    ) -> None:
        self.unit.status = WaitingStatus("Waiting for gateway address")
        if gateway_address := resource_manager.gateway_address(gateway_resource_information.gateway_name):
            self.unit.status = ActiveStatus(f"Gateway addresses: {gateway_address}")
        else:
            self.unit.status = WaitingStatus("Gateway address unavailable")

    @map_k8s_auth_exception
    def available_gateway_classes(self) -> list[str]:
        client = get_client(field_manager=self.app.name, namespace=self.model.name)
        gateway_class_generic_resource = create_global_resource(
            CUSTOM_RESOURCE_GROUP_NAME,
            "v1",
            GATEWAY_CLASS_RESOURCE_NAME,
            GATEWAY_CLASS_PLURAL,
        )
        gateway_classes = tuple(client.list(gateway_class_generic_resource))

        return [
            gateway_class.metadata.name
            for gateway_class in gateway_classes
            if gateway_class.metadata and gateway_class.metadata.name
        ]


if __name__ == "__main__":  # pragma: no cover
    main(SubdomainIntegratorCharm)
