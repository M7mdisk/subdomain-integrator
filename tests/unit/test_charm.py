# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from charm import normalize_dns_label


def test_normalize_dns_label_basic() -> None:
    assert normalize_dns_label("app-test1") == "app-test1"


def test_normalize_dns_label_complex() -> None:
    assert normalize_dns_label("APP_test.1") == "app-test-1"


def test_normalize_dns_label_empty_result() -> None:
    assert normalize_dns_label("---") == "app"
