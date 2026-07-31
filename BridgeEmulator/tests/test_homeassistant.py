"""Home Assistant integration behaviour.

These cover the failure modes that made the integration report "no lamps found"
without ever explaining why.
"""

import pytest

from services import homeAssistantWS as ha
from lights.protocols import homeassistant_ws as ha_protocol
from tests import payloads


@pytest.fixture(autouse=True)
def reset_state():
    ha.reset_for_tests()
    yield
    ha.reset_for_tests()


class TestShouldInclude:
    """The include filter is the single most common reason discovery is empty."""

    def test_untagged_entity_is_excluded_by_default(self):
        assert ha._should_include(payloads.RGB_STRIP_ON, include_by_default=False) is False

    def test_untagged_entity_is_included_when_include_by_default(self):
        assert ha._should_include(payloads.RGB_STRIP_ON, include_by_default=True) is True

    def test_include_tag_wins_when_not_including_by_default(self):
        entity = payloads.tagged(payloads.RGB_STRIP_ON, "include")
        assert ha._should_include(entity, include_by_default=False) is True

    def test_exclude_tag_wins_when_including_by_default(self):
        entity = payloads.tagged(payloads.RGB_STRIP_ON, "exclude")
        assert ha._should_include(entity, include_by_default=True) is False

    def test_non_light_domain_is_never_included(self):
        assert ha._should_include(payloads.SENSOR, include_by_default=True) is False

    def test_switch_domain_is_supported(self):
        entity = {**payloads.PLUG_ON, "entity_id": "switch.arbeitszimmer_stecker1"}
        assert ha._should_include(entity, include_by_default=True) is True

    def test_missing_entity_id_does_not_raise(self):
        """A malformed frame must not take the whole websocket thread down."""
        assert ha._should_include({"state": "on", "attributes": {}}, include_by_default=True) is False


class TestModelMapping:
    def test_onoff_only_maps_to_plug(self):
        assert ha.model_id_for(["onoff"]) == "LOM001"

    def test_brightness_only_maps_to_dimmable(self):
        assert ha.model_id_for(["brightness"]) == "LWB010"

    def test_colour_temp_only_maps_to_ambiance(self):
        assert ha.model_id_for(["color_temp"]) == "LTW001"

    def test_rgb_maps_to_extended_colour(self):
        assert ha.model_id_for(["rgb"]) == "LCT015"

    def test_rgbww_with_colour_temp_maps_to_extended_colour(self):
        """`and` binds tighter than `or`, so this combination used to fall through."""
        assert ha.model_id_for(["rgbww", "color_temp"]) == "LCT015"

    def test_an_entity_without_capabilities_is_not_a_light(self):
        """A Home Assistant install has far more switches than lamps.

        `switch.*` entities report no colour modes at all; mapping them onto a
        plug turned every helper toggle into a bridge light.
        """
        assert ha.model_id_for([]) is None
        assert ha.model_id_for(None) is None

    def test_unknown_modes_map_to_nothing(self):
        assert ha.model_id_for(["white"]) is None


class TestDiscoveryStats:
    """The status page needs to explain an empty discovery, not just report it."""

    def test_counts_seen_included_and_tagged(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([
            payloads.RGB_STRIP_ON,
            payloads.PLUG_OFF,
            payloads.CT_LAMP_ON,
            payloads.SENSOR,
        ])

        stats = ha.status()["discovery"]
        assert stats["entities_seen"] == 3, "sensor.* must not be counted"
        assert stats["entities_included"] == 0
        assert stats["entities_tagged"] == 0
        assert stats["include_by_default"] is False

    def test_include_by_default_includes_everything_supported(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": True})
        ha.record_states([payloads.RGB_STRIP_ON, payloads.PLUG_OFF, payloads.SENSOR])

        stats = ha.status()["discovery"]
        assert stats["entities_seen"] == 2
        assert stats["entities_included"] == 2
        assert set(ha.latest_states) == {
            "light.arbeitszimmer_buttonbox_led_strip",
            "light.arbeitszimmer_stecker1",
        }

    def test_entities_without_capabilities_are_counted_and_named(self):
        """Included switches never become lights - the page has to say so."""
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": True})
        ha.record_states([
            payloads.RGB_STRIP_ON,
            {"entity_id": "switch.arbeitszimmer_stecker1", "state": "off", "attributes": {}},
            {"entity_id": "switch.kuche_autoplay", "state": "off", "attributes": {}},
        ])

        stats = ha.status()["discovery"]
        assert stats["entities_included"] == 3
        assert stats["entities_without_capabilities"] == 2
        assert stats["without_capabilities_sample"] == [
            "switch.arbeitszimmer_stecker1",
            "switch.kuche_autoplay",
        ]

    def test_tagged_entities_are_counted_separately(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([
            payloads.tagged(payloads.RGB_STRIP_ON, "include"),
            payloads.PLUG_OFF,
        ])

        stats = ha.status()["discovery"]
        assert stats["entities_seen"] == 2
        assert stats["entities_tagged"] == 1
        assert stats["entities_included"] == 1


class TestEntityOverrides:
    """Entities picked by hand on the status page.

    Home Assistant reports no capabilities for a `switch.*` entity, but plenty
    of them are lamps that happen to only know on and off.
    """

    def test_every_supported_entity_is_listed_not_only_the_included_ones(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([payloads.RGB_STRIP_ON, payloads.SWITCH_OFF, payloads.SENSOR])

        listing = {entry["entity_id"]: entry for entry in ha.entities()}

        assert set(listing) == {
            "light.arbeitszimmer_buttonbox_led_strip",
            "switch.arbeitszimmer_stecker2",
        }, "the page cannot offer an entity it was never told about"
        assert listing["switch.arbeitszimmer_stecker2"]["included"] is False
        assert listing["switch.arbeitszimmer_stecker2"]["capable"] is False
        assert listing["switch.arbeitszimmer_stecker2"]["name"] == "Arbeitszimmer Stecker 2"

    def test_an_override_includes_an_entity_the_filter_dropped(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([payloads.SWITCH_OFF])
        assert ha.latest_states == {}

        ha.set_override("switch.arbeitszimmer_stecker2", True)

        assert set(ha.latest_states) == {"switch.arbeitszimmer_stecker2"}
        assert ha.status()["discovery"]["entities_included"] == 1

    def test_an_overridden_entity_without_capabilities_becomes_a_plug(self):
        ha.configure({"enabled": True})
        ha.record_states([payloads.SWITCH_OFF])
        assert ha.model_id_for_entity("switch.arbeitszimmer_stecker2", {}) is None

        ha.set_override("switch.arbeitszimmer_stecker2", True)

        assert ha.model_id_for_entity("switch.arbeitszimmer_stecker2", {}) == "LOM001"
        # Now that it maps to a model it is a light, not a counted casualty.
        assert ha.status()["discovery"]["entities_without_capabilities"] == 0

    def test_an_override_excludes_an_entity_the_filter_accepted(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": True})
        ha.record_states([payloads.RGB_STRIP_ON])

        ha.set_override("light.arbeitszimmer_buttonbox_led_strip", False)

        assert ha.latest_states == {}

    def test_an_override_beats_the_diyhue_attribute(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([payloads.tagged(payloads.RGB_STRIP_ON, "exclude")])

        ha.set_override("light.arbeitszimmer_buttonbox_led_strip", True)

        assert set(ha.latest_states) == {"light.arbeitszimmer_buttonbox_led_strip"}

    def test_clearing_an_override_hands_the_decision_back_to_the_filter(self):
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": True})
        ha.record_states([payloads.RGB_STRIP_ON])
        ha.set_override("light.arbeitszimmer_buttonbox_led_strip", False)

        ha.set_override("light.arbeitszimmer_buttonbox_led_strip", None)

        assert set(ha.latest_states) == {"light.arbeitszimmer_buttonbox_led_strip"}
        assert ha.overrides() == {}

    def test_overrides_are_read_from_the_config(self):
        ha.configure({
            "enabled": True,
            "homeAssistantEntities": {"switch.arbeitszimmer_stecker2": True},
        })
        ha.record_states([payloads.SWITCH_OFF])

        assert set(ha.latest_states) == {"switch.arbeitszimmer_stecker2"}

    def test_an_overridden_switch_is_discovered_as_a_light(self, monkeypatch):
        """The end of the chain: override in, lamp out."""
        ha.configure({"enabled": True, "homeAssistantIncludeByDefault": False})
        ha.record_states([payloads.SWITCH_OFF])
        ha.set_override("switch.arbeitszimmer_stecker2", True)
        monkeypatch.setattr(ha, "request_states", ha.status)

        detected = []
        ha.discover(detected)

        assert detected == [{
            "protocol": "homeassistant_ws",
            "name": "Arbeitszimmer Stecker 2",
            "modelid": "LOM001",
            "protocol_cfg": {"entity_id": "switch.arbeitszimmer_stecker2", "ip": "none"},
        }]


class TestConfiguration:
    def test_minimal_config_does_not_raise(self):
        """An upgraded config.yaml only ever gains {"enabled": False}."""
        ha.configure({"enabled": True})
        assert ha.status()["url"] == "ws://127.0.0.1:8123/api/websocket"

    def test_https_config_builds_a_wss_url(self):
        """`use_https` was missing from the global declaration, so wss never applied."""
        ha.configure({
            "enabled": True,
            "homeAssistantIp": "hass.lan",
            "homeAssistantPort": 8123,
            "homeAssistantUseHttps": True,
        })
        assert ha.status()["url"] == "wss://hass.lan:8123/api/websocket"

    def test_token_is_never_exposed_in_status(self):
        ha.configure({"enabled": True, "homeAssistantToken": "supersecret"})
        assert "supersecret" not in repr(ha.status())


class TestDiscoveryErrors:
    def test_discover_without_a_connection_raises_a_typed_error(self):
        """Used to be AttributeError on NoneType, which killed the scan thread."""
        ha.configure({"enabled": True, "homeAssistantIp": "127.0.0.1", "homeAssistantPort": 1})
        detected = []
        with pytest.raises(ha.HomeAssistantUnavailable):
            ha.discover(detected)
        assert detected == []
        assert ha.status()["last_error"]

    def test_empty_state_list_completes_discovery_immediately(self):
        """A falsy `result` used to leave discovery_result unset for the full 60s."""
        ha.discovery_result.clear()
        ha.handle_result_message({"id": 1, "type": "result", "success": True, "result": []},
                                 message_type="getstates")
        assert ha.discovery_result.is_set()
        assert ha.latest_states == {}

    def test_failed_result_records_the_error(self):
        ha.discovery_result.clear()
        ha.handle_result_message(
            {"id": 1, "type": "result", "success": False,
             "error": {"code": "unauthorized", "message": "Unauthorized"}},
            message_type="getstates",
        )
        assert ha.discovery_result.is_set()
        assert "Unauthorized" in ha.status()["last_error"]


class TestStateTranslation:
    def test_plug_off_is_reachable_but_off(self):
        result = ha_protocol.translate_homeassistant_state_to_diyhue_state(
            {"on": True, "bri": 200, "reachable": True}, payloads.PLUG_OFF)
        assert result["on"] is False
        assert result["reachable"] is True

    def test_unavailable_entity_is_unreachable(self):
        result = ha_protocol.translate_homeassistant_state_to_diyhue_state(
            {"on": True, "reachable": True}, payloads.UNAVAILABLE)
        assert result["reachable"] is False
        assert result["on"] is False

    def test_rgb_strip_maps_brightness_and_xy(self):
        result = ha_protocol.translate_homeassistant_state_to_diyhue_state(
            {"on": False, "bri": 1, "xy": [0.0, 0.0], "colormode": "ct"}, payloads.RGB_STRIP_ON)
        assert result["on"] is True
        assert result["bri"] == 36
        assert result["xy"] == [0.178, 0.499]

    def test_rgb_strip_maps_hue_and_saturation(self):
        """hs_color was never translated, so hue/sat stayed at their defaults."""
        result = ha_protocol.translate_homeassistant_state_to_diyhue_state(
            {"on": False, "hue": 0, "sat": 0}, payloads.RGB_STRIP_ON)
        # HA reports degrees 0-360 and percent 0-100; Hue uses 0-65535 and 0-254.
        assert result["hue"] == pytest.approx(152.432 / 360 * 65535, abs=1)
        assert result["sat"] == pytest.approx(76.289 / 100 * 254, abs=1)

    def test_colour_temperature_is_read_from_kelvin(self):
        """The outbound path migrated to color_temp_kelvin; inbound must match."""
        result = ha_protocol.translate_homeassistant_state_to_diyhue_state(
            {"on": False, "ct": 200, "colormode": "xy"}, payloads.CT_LAMP_ON)
        assert result["colormode"] == "ct"
        assert result["ct"] == pytest.approx(1000000 / 3000, abs=1)

    def test_missing_entity_is_reported_unreachable(self):
        """latest_states lookups used to KeyError when an entity vanished from HA."""
        class FakeLight:
            protocol_cfg = {"entity_id": "light.gone"}
            state = {"on": True, "reachable": True}

        result = ha_protocol.get_light_state(FakeLight())
        assert result["reachable"] is False
