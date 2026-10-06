"""Group brightness aggregation must tolerate lights without brightness.

Home Assistant switches exposed as LOM001 plugs have no brightness
capability, yet their state can still carry a ``bri: None`` entry (the
sync loop stores whatever a protocol reports). ``Group.update_state``
summed that None and turned every ``GET /api/{user}/groups/{id}`` into
a 500, which the web UI surfaced as an Axios error on each poll.
"""

from HueObjects.Group import Group
from HueObjects.Light import Light

# Group.add_light stores a weakref; keep strong references alive here,
# mirroring production where bridgeConfig["lights"] owns them.
_KEPT = []


def _light(id_v1, modelid, state):
    light = Light({"id_v1": id_v1, "name": "light " + id_v1, "modelid": modelid,
                   "state": state, "protocol": "dummy"})
    _KEPT.append(light)
    return light


class TestGroupUpdateState:
    def test_plug_without_bri_key(self):
        group = Group({"id_v1": "0"})
        group.add_light(_light("1", "LOM001", {"on": True, "reachable": True}))
        assert group.update_state() == {"all_on": True, "any_on": True, "avr_bri": 0}

    def test_bri_none_does_not_raise(self):
        group = Group({"id_v1": "0"})
        group.add_light(_light("1", "LOM001", {"on": True, "bri": None, "reachable": True}))
        state = group.update_state()
        assert state["any_on"] is True
        assert state["avr_bri"] == 0

    def test_average_ignores_bri_none(self):
        group = Group({"id_v1": "0"})
        group.add_light(_light("1", "LWB010", {"on": True, "bri": 127, "reachable": True}))
        group.add_light(_light("2", "LOM001", {"on": True, "bri": None, "reachable": True}))
        state = group.update_state()
        assert state["any_on"] is True
        # only the dimmable light counts towards the average
        assert state["avr_bri"] == 50

    def test_off_light_keeps_all_on_false(self):
        group = Group({"id_v1": "0"})
        group.add_light(_light("1", "LWB010", {"on": True, "bri": 254, "reachable": True}))
        group.add_light(_light("2", "LOM001", {"on": False, "bri": None, "reachable": True}))
        state = group.update_state()
        assert state == {"all_on": False, "any_on": True, "avr_bri": 100}
