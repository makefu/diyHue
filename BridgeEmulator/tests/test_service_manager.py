"""Runtime enable/disable of the connection-owning integrations."""

import threading

import pytest

from services import serviceManager, statusRegistry


class FakeService:
    """Stands in for mqtt/deconz: a blocking loop that stop() unblocks."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.start_count = 0
        self.configs = []
        self.fail_with = None

    def start(self, config):
        self.start_count += 1
        self.configs.append(config)
        if self.fail_with is not None:
            raise self.fail_with
        self.started.set()
        self.release.wait(timeout=5)

    def stop(self):
        self.release.set()


@pytest.fixture
def fake(monkeypatch):
    service = FakeService()
    monkeypatch.setitem(serviceManager.SERVICES, "mqtt", serviceManager.ManagedService(
        name="mqtt", config_key="mqtt", start=service.start, stop=service.stop))
    # A thread left over from a previous test belongs to a different stub and
    # would not respond to this one's stop().
    serviceManager.reset_for_tests()
    statusRegistry.reset()
    yield service
    service.stop()
    serviceManager.reset_for_tests()


def config(**mqtt_settings):
    return {"config": {"mqtt": {"enabled": False, **mqtt_settings},
                       "homeassistant": {"enabled": False},
                       "deconz": {"enabled": False}}}


class TestReconcile:
    def test_enabled_service_is_started(self, fake):
        bridge = config(enabled=True)

        serviceManager.apply(bridge, changed_keys={"mqtt"})

        assert fake.started.wait(timeout=5)
        assert serviceManager.is_running("mqtt") is True

    def test_disabling_stops_the_service(self, fake):
        bridge = config(enabled=True)
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        bridge["config"]["mqtt"]["enabled"] = False
        serviceManager.apply(bridge, changed_keys={"mqtt"})

        assert serviceManager.is_running("mqtt") is False

    def test_apply_is_idempotent(self, fake):
        bridge = config(enabled=True)
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        serviceManager.apply(bridge)

        assert fake.start_count == 1, "a running service must not be restarted"

    def test_changed_settings_restart_the_service(self, fake):
        """A new broker address has to be picked up, not just persisted."""
        bridge = config(enabled=True, mqttServer="broker.old")
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        bridge["config"]["mqtt"]["mqttServer"] = "broker.new"
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        assert fake.start_count == 2
        assert fake.configs[-1]["mqttServer"] == "broker.new"

    def test_untouched_services_are_left_alone(self, fake):
        bridge = config(enabled=True)
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        serviceManager.apply(bridge, changed_keys={"homeassistant"})

        assert fake.start_count == 1


class TestErrorReporting:
    def test_a_service_that_dies_reports_why(self, fake):
        fake.fail_with = ConnectionRefusedError("broker refused the connection")
        bridge = config(enabled=True)

        serviceManager.apply(bridge, changed_keys={"mqtt"})
        for _ in range(50):
            if serviceManager.status("mqtt").get("error"):
                break
            threading.Event().wait(0.05)

        state = serviceManager.status("mqtt")
        assert state["enabled"] is True
        assert "broker refused the connection" in state["error"]

    def test_status_reports_enabled_and_running(self, fake):
        bridge = config(enabled=True)
        serviceManager.apply(bridge, changed_keys={"mqtt"})
        assert fake.started.wait(timeout=5)

        state = serviceManager.status("mqtt")
        assert state["enabled"] is True
        assert state["running"] is True


class TestRegistryNamespacing:
    def test_service_state_survives_an_integration_status_update(self):
        """An integration owns its own registry key; the manager must not clash.

        homeAssistantWS republishes its whole state on every connection
        attempt, which used to wipe the manager's running/enabled fields.
        """
        from services import homeAssistantWS

        statusRegistry.reset()
        bridge = config()
        bridge["config"]["homeassistant"] = {"enabled": True,
                                             "homeAssistantIp": "127.0.0.1",
                                             "homeAssistantPort": 1}
        try:
            serviceManager.apply(bridge, changed_keys={"homeassistant"})
            homeAssistantWS._set_status(connected=False, last_error="boom")

            managed = serviceManager.status("homeassistant")
            assert managed["enabled"] is True
            assert "running" in managed
            assert homeAssistantWS.status()["last_error"] == "boom"
        finally:
            homeAssistantWS.reset_for_tests()


class TestSetEnabled:
    def test_set_enabled_writes_the_config_and_applies_it(self, fake):
        bridge = config()

        serviceManager.set_enabled(bridge, "mqtt", True)

        assert bridge["config"]["mqtt"]["enabled"] is True
        assert fake.started.wait(timeout=5)

    def test_unknown_service_is_rejected(self):
        with pytest.raises(KeyError):
            serviceManager.set_enabled(config(), "not-a-service", True)
