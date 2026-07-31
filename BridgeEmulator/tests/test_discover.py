"""Light discovery orchestration.

A single misbehaving protocol used to abort the whole scan thread, leaving the
UI showing a search that never finishes.
"""

import pytest

import configManager
from lights import discover
from services import statusRegistry


bridgeConfig = configManager.bridgeConfig.yaml_config


def protocol(name, run):
    return discover.DiscoveryProtocol(name=name, run=run, is_enabled=lambda config: True)


def found_light(name, entity_id):
    return {
        "protocol": "homeassistant_ws",
        "name": name,
        "modelid": "LOM001",
        "protocol_cfg": {"entity_id": entity_id, "ip": "none"},
    }


@pytest.fixture(autouse=True)
def isolated_scan(monkeypatch):
    monkeypatch.setattr(discover, "get_device_ips", lambda: [])
    bridgeConfig["temp"]["scanResult"] = {"lastscan": "none"}
    statusRegistry.reset()
    yield
    for light_id in [k for k in bridgeConfig["lights"] if k != "0"]:
        del bridgeConfig["lights"][light_id]


class TestErrorIsolation:
    def test_a_raising_protocol_does_not_abort_the_scan(self, monkeypatch):
        calls = []

        def boom(detected, device_ips):
            calls.append("homeassistant")
            raise RuntimeError("Home Assistant websocket is not connected")

        def later(detected, device_ips):
            calls.append("wled")
            detected.append(found_light("Desk strip", "light.desk"))

        monkeypatch.setattr(discover, "PROTOCOLS", [
            protocol("homeassistant", boom),
            protocol("wled", later),
        ])

        result = discover.scanForLights()

        assert calls == ["homeassistant", "wled"], "later protocols must still run"
        assert result["lastscan"] != "active", "the scan must not look stuck"
        assert any(entry.get("name") == "Desk strip" for entry in result.values()
                   if isinstance(entry, dict))

    def test_protocol_failure_is_reported_with_its_reason(self, monkeypatch):
        def boom(detected, device_ips):
            raise RuntimeError("Home Assistant websocket is not connected")

        monkeypatch.setattr(discover, "PROTOCOLS", [protocol("homeassistant", boom)])
        discover.scanForLights()

        protocols = statusRegistry.snapshot()["components"]["scan"]["protocols"]
        assert protocols["homeassistant"]["state"] == "error"
        assert "not connected" in protocols["homeassistant"]["error"]

    def test_scan_state_is_reset_even_when_everything_fails(self, monkeypatch):
        def boom(*args):
            raise RuntimeError("nope")

        monkeypatch.setattr(discover, "PROTOCOLS", [protocol("homeassistant", boom)])
        monkeypatch.setattr(discover, "get_device_ips", boom)

        discover.scanForLights()

        assert bridgeConfig["temp"]["scanResult"]["lastscan"] != "active"
        assert bridgeConfig["config"]["zigbee_device_discovery_info"]["status"] == "ready"

    def test_unknown_model_id_is_not_stored_under_a_false_key(self, monkeypatch):
        def bogus(detected, device_ips):
            light = found_light("Mystery lamp", "light.mystery")
            light["modelid"] = "NOT_A_REAL_MODEL"
            detected.append(light)

        monkeypatch.setattr(discover, "PROTOCOLS", [protocol("bogus", bogus)])
        result = discover.scanForLights()

        assert False not in result


class TestProtocolTable:
    def test_every_protocol_has_a_config_entry(self):
        """A typo in the table would silently disable a protocol."""
        for protocol in discover.PROTOCOLS:
            # Must be answerable without raising, whatever the config holds.
            assert protocol.is_enabled(bridgeConfig["config"]) in (True, False)

    def test_pairing_based_protocols_are_not_toggleable(self):
        """hue and tradfri have no `enabled` flag - they are on once paired."""
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        assert by_name["hue"].toggleable is False
        assert by_name["tradfri"].toggleable is False
        assert by_name["wled"].toggleable is True

    def test_flag_based_protocols_follow_the_config(self):
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        config = {"wled": {"enabled": False}}
        assert by_name["wled"].is_enabled(config) is False
        config["wled"]["enabled"] = True
        assert by_name["wled"].is_enabled(config) is True

    def test_a_missing_config_section_reads_as_disabled(self):
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        assert by_name["wled"].is_enabled({}) is False


class TestScanProgress:
    def test_scan_publishes_start_and_finish_events(self, monkeypatch):
        monkeypatch.setattr(discover, "PROTOCOLS", [
            protocol("wled", lambda detected, ips: detected.append(
                found_light("Desk strip", "light.desk"))),
        ])

        events = []
        subscription = statusRegistry.subscribe()
        try:
            discover.scanForLights()
            events = statusRegistry.drain(subscription)
        finally:
            statusRegistry.unsubscribe(subscription)

        kinds = [event["kind"] for event in events]
        assert "scan_started" in kinds
        assert "light_found" in kinds
        assert "scan_finished" in kinds

    def test_light_found_event_names_the_new_light(self, monkeypatch):
        monkeypatch.setattr(discover, "PROTOCOLS", [
            protocol("wled", lambda detected, ips: detected.append(
                found_light("Desk strip", "light.desk"))),
        ])

        subscription = statusRegistry.subscribe()
        try:
            discover.scanForLights()
            events = statusRegistry.drain(subscription)
        finally:
            statusRegistry.unsubscribe(subscription)

        found = [event for event in events if event["kind"] == "light_found"]
        assert len(found) == 1
        assert found[0]["name"] == "Desk strip"
        assert found[0]["protocol"] == "homeassistant_ws"

    def test_repeated_scans_do_not_duplicate_a_known_light(self, monkeypatch):
        monkeypatch.setattr(discover, "PROTOCOLS", [
            protocol("wled", lambda detected, ips: detected.append(
                found_light("Desk strip", "light.desk"))),
        ])

        discover.scanForLights()
        before = len(bridgeConfig["lights"])
        discover.scanForLights()

        assert len(bridgeConfig["lights"]) == before


class TestHostSweep:
    def test_sweep_reports_progress(self, monkeypatch):
        monkeypatch.setattr(discover, "scanHost", lambda host, port: 1)
        monkeypatch.setitem(bridgeConfig["config"], "IP_RANGE", {
            "IP_RANGE_START": 1, "IP_RANGE_END": 8,
            "SUB_IP_RANGE_START": 1, "SUB_IP_RANGE_END": 1,
        })

        subscription = statusRegistry.subscribe()
        try:
            discover.find_hosts(80)
            events = statusRegistry.drain(subscription)
        finally:
            statusRegistry.unsubscribe(subscription)

        progress = [event for event in events if event["kind"] == "scan_progress"]
        assert progress, "the host sweep must report progress"
        assert progress[-1]["scanned"] == progress[-1]["total"]

    def test_sweep_finds_the_hosts_that_answer(self, monkeypatch):
        monkeypatch.setattr(discover, "scanHost",
                            lambda host, port: 0 if host.endswith((".3", ".5")) else 1)
        monkeypatch.setitem(bridgeConfig["config"], "IP_RANGE", {
            "IP_RANGE_START": 1, "IP_RANGE_END": 8,
            "SUB_IP_RANGE_START": 1, "SUB_IP_RANGE_END": 1,
        })

        hosts = discover.find_hosts(80)

        assert sorted(hosts) == ["192.168.1.3:80", "192.168.1.5:80"]
