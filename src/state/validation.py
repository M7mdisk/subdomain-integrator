# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for functions containing charm utilities."""

import functools
import logging
import typing

import ops
from ops.model import SecretNotFoundError

import client
from resource_manager.resource_manager import InvalidResourceError
from state.exception import CharmStateValidationBaseError
from state.http_route import IngressIntegrationDataValidationError

logger = logging.getLogger(__name__)

C = typing.TypeVar("C", bound=ops.CharmBase)


class InvalidCertificateError(Exception):
    """Exception raised when certificates is invalid."""


def validate_config_and_integration(
    defer: bool = False,
) -> typing.Callable[
    [typing.Callable[[C, typing.Any], None]], typing.Callable[[C, typing.Any], None]
]:
    """Create a decorator that puts the charm in blocked state if config is wrong."""

    def decorator(
        method: typing.Callable[[C, typing.Any], None],
    ) -> typing.Callable[[C, typing.Any], None]:
        @functools.wraps(method)
        def wrapper(instance: C, *args: typing.Any) -> None:
            try:
                return method(instance, *args)
            except (
                CharmStateValidationBaseError,
                IngressIntegrationDataValidationError,
            ) as exc:
                if defer:
                    event: ops.EventBase
                    event, *_ = args
                    event.defer()
                logger.exception("Error setting up charm state component: %s", str(exc))
                instance.unit.status = ops.BlockedStatus(str(exc))
                _clean_up_resources_in_blocked_state(instance)
                return None
            except InvalidResourceError:
                logger.exception("Error creating kubernetes resource")
                raise
            except (InvalidCertificateError, SecretNotFoundError):
                logger.exception("TLS certificates error.")
                raise

        return wrapper

    return decorator


# pylint: disable=broad-exception-caught
def _clean_up_resources_in_blocked_state(instance: ops.CharmBase) -> None:
    try:
        client.cleanup_all_resources(
            client.get_client(field_manager=instance.app.name, namespace=instance.model.name),
            client.application_label_selector(instance.app.name),
        )
    except Exception:
        logger.exception("Error raised during cleanup while handling another error, skipping.")
