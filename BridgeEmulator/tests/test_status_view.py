"""What the status page reports about the bridge's own light list.

Home Assistant entities passing the include filter are *candidates*; they only
become Hue lights when a discovery scan registers them.  Reporting the filter
counts alone made a bridge with 179 included entities and no lights look
healthy, so the state has to carry both numbers.
"""

import pytest

import configManager
from flaskUI.core import views as core_views
from flaskUI.status import views
from lights import discover
from services import homeAssistantWS, statusRegistry
from tests import payloads


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


class TestEntityExposure:
    """Ticking an entity on the status page has to produce a lamp there and then.

    Making the user run a scan afterwards is what made included entities look
    lost, and a switch has no capabilities to infer a model from at all.
    """

    @pytest.fixture(autouse=True)
    def home_assistant(self):
        homeAssistantWS.reset_for_tests()
        homeAssistantWS.configure({"enabled": True})
        homeAssistantWS.record_states([payloads.SWITCH_OFF, payloads.RGB_STRIP_ON])
        yield
        homeAssistantWS.reset_for_tests()
        bridgeConfig["config"].get("homeassistant", {}).pop("homeAssistantEntities", None)

    def test_an_entity_without_capabilities_can_be_exposed_as_a_plug(self):
        light_id = views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)

        light = bridgeConfig["lights"][light_id]
        assert light.modelid == "LOM001"
        assert light.name == "Arbeitszimmer Stecker 2"
        assert light.protocol_cfg["entity_id"] == "switch.arbeitszimmer_stecker2"

    def test_the_choice_is_persisted_in_the_config(self):
        views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)

        stored = bridgeConfig["config"]["homeassistant"]["homeAssistantEntities"]
        assert stored == {"switch.arbeitszimmer_stecker2": True}

    def test_the_listing_reports_which_entities_are_exposed(self):
        views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)

        listing = {entry["entity_id"]: entry for entry in views._entity_listing()}

        assert listing["switch.arbeitszimmer_stecker2"]["exposed"] is True
        assert listing["light.arbeitszimmer_buttonbox_led_strip"]["exposed"] is False

    def test_exposing_twice_does_not_create_a_second_light(self):
        first = views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)
        again = views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)

        assert first == again
        assert len(views._home_assistant_lights()) == 1

    def test_unticking_removes_the_light_again(self):
        light_id = views.set_entity_exposure("switch.arbeitszimmer_stecker2", True)

        views.set_entity_exposure("switch.arbeitszimmer_stecker2", False)

        assert light_id not in bridgeConfig["lights"]
        assert bridgeConfig["config"]["homeassistant"]["homeAssistantEntities"] == {
            "switch.arbeitszimmer_stecker2": False}

    def test_an_unknown_entity_is_rejected(self):
        with pytest.raises(KeyError):
            views.set_entity_exposure("switch.not_reported_by_home_assistant", True)


class TestStatusLinkInjection:
    """The bundled app is a prebuilt archive, so the link is added on the way out."""

    def test_the_link_is_injected_before_the_closing_body_tag(self):
        page = core_views.with_status_link("<html><body><div id=root></div></body></html>")
        assert 'href="/status/"' in page
        assert page.index('href="/status/"') < page.index("</body>")

    def test_a_page_without_a_body_tag_is_left_alone(self):
        original = "<html><div id=root></div></html>"
        assert core_views.with_status_link(original) == original

    def test_the_link_is_not_added_twice(self):
        once = core_views.with_status_link("<html><body></body></html>")
        assert core_views.with_status_link(once).count('href="/status/"') == 1
