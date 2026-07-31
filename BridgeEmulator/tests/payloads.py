"""Real Home Assistant state payloads.

Captured verbatim from a live Home Assistant 2026.7 instance via
``GET /api/states`` so the translation tests exercise the attribute shapes HA
actually emits rather than an idealised subset.
"""

# light.arbeitszimmer_stecker1 - a smart plug exposed as a light; onoff only.
PLUG_OFF = {
    "entity_id": "light.arbeitszimmer_stecker1",
    "state": "off",
    "attributes": {
        "supported_color_modes": ["onoff"],
        "color_mode": None,
        "friendly_name": "arbeitszimmer_stecker1 arbeitszimmer_stecker1",
        "supported_features": 0,
    },
}

PLUG_ON = {
    "entity_id": "light.arbeitszimmer_stecker1",
    "state": "on",
    "attributes": {
        "supported_color_modes": ["onoff"],
        "color_mode": "onoff",
        "friendly_name": "arbeitszimmer_stecker1 arbeitszimmer_stecker1",
        "supported_features": 0,
    },
}

# light.arbeitszimmer_buttonbox_led_strip - an RGB strip. Note that HA reports
# rgb/hs/xy attributes even though supported_color_modes only lists "rgb".
RGB_STRIP_ON = {
    "entity_id": "light.arbeitszimmer_buttonbox_led_strip",
    "state": "on",
    "attributes": {
        "supported_color_modes": ["rgb"],
        "color_mode": "rgb",
        "brightness": 36,
        "hs_color": [152.432, 76.289],
        "rgb_color": [46, 194, 126],
        "xy_color": [0.178, 0.499],
        "friendly_name": "Arbeitszimmer Buttonbox Arbeitszimmer Buttonbox LED-Strip",
        "supported_features": 40,
    },
}

# light.kinderzimmer_mi_desk_lamp_1s_light - colour-temperature only.
CT_LAMP_ON = {
    "entity_id": "light.kinderzimmer_mi_desk_lamp_1s_light",
    "state": "on",
    "attributes": {
        "supported_color_modes": ["color_temp"],
        "color_mode": "color_temp",
        "brightness": 180,
        "color_temp_kelvin": 3000,
        "color_temp": 333,
        "min_color_temp_kelvin": 2000,
        "max_color_temp_kelvin": 6535,
        "friendly_name": "Kinderzimmer Mi Desk Lamp 1S Light",
        "supported_features": 44,
    },
}

# light.led - an entity whose device is offline.
UNAVAILABLE = {
    "entity_id": "light.led",
    "state": "unavailable",
    "attributes": {
        "supported_color_modes": ["rgb"],
        "friendly_name": "Tagreader1-3C7627 LED",
    },
}

# switch.arbeitszimmer_stecker2 - a wall plug. Home Assistant reports no colour
# modes at all for switch entities, so nothing infers that this is a lamp; only
# the user can say so.
SWITCH_OFF = {
    "entity_id": "switch.arbeitszimmer_stecker2",
    "state": "off",
    "attributes": {
        "friendly_name": "Arbeitszimmer Stecker 2",
        "device_class": "outlet",
    },
}

# Not a light or switch - must never be considered for discovery.
SENSOR = {
    "entity_id": "sensor.arbeitszimmer_temperature",
    "state": "21.4",
    "attributes": {"friendly_name": "Arbeitszimmer Temperature"},
}


def tagged(payload, flag):
    """Return a copy of ``payload`` carrying the ``diyhue`` include/exclude flag."""
    copy = {**payload, "attributes": {**payload["attributes"], "diyhue": flag}}
    return copy
