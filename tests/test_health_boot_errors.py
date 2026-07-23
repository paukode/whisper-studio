"""Boot failures must surface in the /health payload."""

import pytest

from server.infrastructure import boot_status


@pytest.fixture(autouse=True)
def clean_registry():
    boot_status.BOOT_ERRORS.clear()
    yield
    boot_status.BOOT_ERRORS.clear()


def test_healthy_when_no_boot_errors():
    assert boot_status.health_payload() == {"status": "ok"}


def test_degraded_with_recorded_error():
    boot_status.record_boot_error("server.executors.web", "No module named requests")
    payload = boot_status.health_payload()
    assert payload["status"] == "degraded"
    assert payload["boot_errors"] == [
        {"component": "server.executors.web", "error": "No module named requests"}
    ]


def test_payload_is_a_copy():
    boot_status.record_boot_error("bedrock_pricing", "throttled")
    payload = boot_status.health_payload()
    payload["boot_errors"].append({"component": "fake", "error": "fake"})
    assert len(boot_status.BOOT_ERRORS) == 1
