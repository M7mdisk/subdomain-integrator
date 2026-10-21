# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ops.testing import Harness

from charm import SubdomainIntegratorCharm
from resource_manager.gateway import GatewayResourceDefinition
from resource_manager.secret import SecretResourceDefinition
from state.config import CharmConfig
from state.gateway import GatewayResourceInformation
from state.tls import TLSInformation, TLSInformationInvalidError

BASE_DOMAIN = "marketplace.demos.canonical.com"
WILDCARD_HOSTNAME = f"*.{BASE_DOMAIN}"


@pytest.fixture
def harness():
    """Provide a leader charm without Kubernetes reconciliation."""
    harness = Harness(SubdomainIntegratorCharm)
    harness.set_leader(True)
    harness.update_config({"base-domain": BASE_DOMAIN, "gateway-class": "test-gateway"})
    with patch.object(SubdomainIntegratorCharm, "_reconcile"):
        harness.begin()
        yield harness
    harness.cleanup()


@pytest.mark.parametrize("base_domain", ["demos.canonical.com", BASE_DOMAIN])
def test_certificate_request_is_only_wildcard(harness, base_domain):
    """
    arrange: configure a base domain at either nesting depth.
    act: construct the certificate requests.
    assert: only the wildcard hostname is requested.
    """
    harness.update_config({"base-domain": base_domain})

    requests = harness.charm._get_certificate_requests()

    assert len(requests) == 1
    assert requests[0].common_name == f"*.{base_domain}"
    assert requests[0].sans_dns == frozenset({f"*.{base_domain}"})


def test_no_certificate_request_without_base_domain(harness):
    """
    arrange: clear the base domain.
    act: construct the certificate requests.
    assert: no certificate is requested.
    """
    harness.update_config({"base-domain": ""})

    assert harness.charm._get_certificate_requests() == []


@pytest.mark.parametrize("app_names", [[], ["prs-test"], ["prs-test", "another-app"]])
def test_haproxy_publishes_only_wildcard(harness, app_names):
    """
    arrange: seed individual hostnames and relate zero, one, or multiple applications.
    act: publish the HAProxy route requirements.
    assert: only the wildcard hostname remains, pointing to the gateway.
    """
    relation_id = harness.add_relation("haproxy-route", "haproxy")
    harness.update_relation_data(
        relation_id,
        harness.charm.app.name,
        {
            "hostname": json.dumps(f"old.{BASE_DOMAIN}"),
            "additional_hostnames": json.dumps([f"another-old.{BASE_DOMAIN}"]),
        },
    )
    for app_name in app_names:
        harness.add_relation("ingress", app_name)
    manager = Mock()
    manager.gateway_address.return_value = "10.0.0.1"
    config = CharmConfig(
        base_domain=BASE_DOMAIN, gateway_class_name="test-gateway", enforce_https=False
    )
    information = GatewayResourceInformation.from_charm(harness.charm)

    harness.charm._provide_haproxy_route_requirements(manager, information, config)

    data = harness.get_relation_data(relation_id, harness.charm.app.name)
    assert json.loads(data["hostname"]) == WILDCARD_HOSTNAME
    assert json.loads(data.get("additional_hostnames", "[]")) == []
    assert json.loads(data["hosts"]) == ["10.0.0.1"]
    assert json.loads(data["ports"]) == [80]


def test_application_routing_keeps_individual_hostname(harness):
    """
    arrange: relate an application under the wildcard base domain.
    act: derive its internal gateway hostname.
    assert: the hostname still identifies the individual application.
    """
    relation_id = harness.add_relation("ingress", "prs-test")
    relation = harness.model.get_relation("ingress", relation_id)
    assert relation is not None
    with patch.object(
        harness.charm._ingress_provider,
        "get_data",
        return_value=SimpleNamespace(app=SimpleNamespace(name="prs-test")),
    ):
        assert harness.charm._relation_hostname(relation, BASE_DOMAIN) == (
            f"prs-test.{BASE_DOMAIN}"
        )


def test_dns_relation_publishes_one_wildcard_record(harness):
    """
    arrange: relate a DNS provider and supply the gateway address.
    act: publish the requested DNS records.
    assert: exactly one wildcard address record is requested.
    """
    relation_id = harness.add_relation("dns-record", "bind")
    manager = Mock()
    manager.gateway_address.return_value = "10.0.0.1"
    information = GatewayResourceInformation.from_charm(harness.charm)

    harness.charm._update_dns_record_relation(manager, BASE_DOMAIN, information)

    data = harness.get_relation_data(relation_id, harness.charm.app.name)
    entries = json.loads(data["dns_entries"])
    assert len(entries) == 1
    assert entries[0]["domain"] == BASE_DOMAIN
    assert entries[0]["host_label"] == "*"
    assert entries[0]["record_type"] == "A"
    assert entries[0]["record_data"] == "10.0.0.1"


@pytest.mark.parametrize(
    "common_name", [WILDCARD_HOSTNAME, BASE_DOMAIN, f"prs-test.{BASE_DOMAIN}"]
)
def test_gateway_uses_only_wildcard_certificate(harness, common_name):
    """
    arrange: supply a wildcard, base-domain, or individual application certificate.
    act: construct gateway TLS information.
    assert: only the wildcard is accepted, with valid Kubernetes Secret names.
    """
    harness.add_relation("certificates", "certificate-provider")
    certificate = SimpleNamespace(common_name=common_name)
    certificates = Mock()
    certificates.get_provider_certificates.return_value = [
        SimpleNamespace(certificate=certificate, chain=[certificate])
    ]
    certificates.private_key = "test-key"

    if common_name != WILDCARD_HOSTNAME:
        with pytest.raises(TLSInformationInvalidError):
            TLSInformation.from_charm(harness.charm, BASE_DOMAIN, certificates)
        return

    information = TLSInformation.from_charm(harness.charm, BASE_DOMAIN, certificates)
    secret = SecretResourceDefinition.from_tls_information(information)
    gateway = GatewayResourceDefinition(
        GatewayResourceInformation.from_charm(harness.charm),
        CharmConfig(
            base_domain=BASE_DOMAIN, gateway_class_name="test-gateway", enforce_https=True
        ),
        information,
    )

    assert information.hostname == BASE_DOMAIN
    assert information.tls_certs[BASE_DOMAIN] == str(certificate)
    assert information.tls_keys[BASE_DOMAIN] == "test-key"
    assert "*" not in secret.secret_resource_name
    assert gateway.tls_secret_name == secret.secret_resource_name
