"""What the status page reports about the bridge's own light list.

Home Assistant entities passing the include filter are *candidates*; they only
become Hue lights when a discovery scan registers them.  Reporting the filter
counts alone made a bridge with 179 included entities and no lights look
healthy, so the state has to carry both numbers.
"""

import pytest

import configManager
from flaskUI.status import views
from lights import discover
from services import statusRegistry


bridgeConfig = configManager.bridgeConfig.yaml_config


@pytest.fixture(autouse=True)
def clean_lights():
    statusRegistry.reset()
    yield
    for light_id in [k for k in bridgeConfig["lights"] if k != "0"]:
        del bridgeConfig["lights"][light_id]


def add_light(name, modelid, protocol, protocol_cfg):
    return discover.addNewLight(modelid, name, protocol, protocol_cfg)


class TestProtocolLightNames:
    def test_home_assistant_lights_are_attributed_to_the_integration(self):
        """The discovery protocol is `homeassistant`, its lights `homeassistant_ws`."""
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        assert "homeassistant_ws" in by_name["homeassistant"].light_protocols

    def test_a_protocol_defaults_to_its_own_name(self):
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        assert by_name["wled"].light_protocols == ("wled",)

    def test_native_multi_covers_the_names_its_lights_are_stored_under(self):
        by_name = {protocol.name: protocol for protocol in discover.PROTOCOLS}
        assert set(by_name["native_multi"].light_protocols) >= {"native_multi", "native_single"}


class TestRegisteredLightCounts:
    def test_state_counts_lights_per_integration(self):
        add_light("Desk plug", "LOM001", "homeassistant_ws",
                  {"entity_id": "light.desk", "ip": "none"})

        state = views._build_state()

        assert state["protocols"]["homeassistant"]["lights"] == 1
        assert state["protocols"]["wled"]["lights"] == 0

    def test_included_entities_without_lights_are_called_out(self):
        """The exact shape of the reported problem: included, but never scanned."""
        views.homeAssistantWS._set_discovery_status(entities_seen=179, entities_included=179)

        state = views._build_state()

        assert state["homeassistant"]["discovery"]["entities_included"] == 179
        assert state["protocols"]["homeassistant"]["lights"] == 0

    def test_a_scan_is_reported_as_never_having_run(self):
        state = views._build_state()
        assert state["scan"].get("lastscan") is None

