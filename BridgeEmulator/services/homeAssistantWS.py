import logManager
import json
import threading
import time
from datetime import datetime, timezone
from ws4py.client.threadedclient import WebSocketClient

from services import statusRegistry

logging = logManager.logger.get_logger(__name__)


discovery_timeout_seconds = 60
discovery_result = threading.Event()
homeassistant_token = ''
homeassistant_url = 'ws://127.0.0.1:8123/api/websocket'
homeassistant_ws_client = None
include_by_default = False

# Reconnect backoff, in seconds. The supervisor thread keeps retrying for as
# long as the integration is enabled, so a Home Assistant restart no longer
# leaves diyHue silently disconnected until the next light command.
RECONNECT_INITIAL_DELAY = 2
RECONNECT_MAX_DELAY = 60

# Home Assistant colour modes, mirrored from homeassistant/components/light.
UNKNOWN = "unknown"
ONOFF = "onoff"
BRIGHTNESS = "brightness"
COLOR_TEMP = "color_temp"
HS = "hs"
XY = "xy"
RGB = "rgb"
RGBW = "rgbw"
RGBWW = "rgbww"
WHITE = "white"

COLOUR_MODES = (HS, XY, RGB, RGBW, RGBWW)

SUPPORTED_DOMAINS = ("light.", "switch.")

# How many rejected entity ids to keep for the status page, so the user can see
# concrete examples of what the include filter dropped.
EXCLUDED_SAMPLE_SIZE = 5

# This is Home Assistant States so looks like this:
# {
#   'entity_id': 'light.my_light',
#   'state': 'on',
#   'attributes': {
#        'min_mireds': 153,
#        'max_mireds': 500,
#        'effect_list': ['colorloop', 'random'],
#        'brightness': 254,
#        'hs_color': [291.687, 65.098],
#        'rgb_color': [232, 89, 255],
#        'xy_color': [0.348, 0.168],
#        'is_hue_group': True,
#        'friendly_name': 'My Light',
#        'supported_features': 63
#   },
#   'last_changed': '2019-01-09T10:35:39.148462+00:00',
#    'last_updated': '2019-01-09T10:35:39.148462+00:00',
#    'context': {'id': 'X', 'parent_id': None, 'user_id': None}
# }
latest_states = {}

_state_lock = threading.Lock()
_supervisor = None
_running = False
_wake_supervisor = threading.Event()


def _blank_status():
    return {
        "enabled": False,
        "url": homeassistant_url,
        "token_configured": False,
        "connected": False,
        "authenticated": False,
        "last_error": None,
        "last_connect_attempt": None,
        "discovery": {
            "include_by_default": False,
            "entities_seen": 0,
            "entities_included": 0,
            "entities_tagged": 0,
            "excluded_sample": [],
            "last_discovery": None,
        },
    }


_status = _blank_status()


class HomeAssistantUnavailable(Exception):
    """Raised instead of an AttributeError when there is no usable connection.

    Discovery and the light protocol both run inside worker threads; a typed
    error lets the caller report *why* Home Assistant is unusable rather than
    dying on ``'NoneType' object has no attribute ...``.
    """


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_status(**fields):
    with _state_lock:
        _status.update(fields)
        snapshot = json.loads(json.dumps(_status))
    statusRegistry.replace("homeassistant", snapshot)
    return snapshot


def _set_discovery_status(**fields):
    with _state_lock:
        _status["discovery"].update(fields)
        snapshot = json.loads(json.dumps(_status))
    statusRegistry.replace("homeassistant", snapshot)
    return snapshot


def status():
    """Current integration state. Never contains the access token."""
    with _state_lock:
        return json.loads(json.dumps(_status))


def reset_for_tests():
    """Return the module to its freshly imported state."""
    global homeassistant_ws_client, homeassistant_token, homeassistant_url
    global include_by_default, _status, _running
    stop()
    homeassistant_ws_client = None
    homeassistant_token = ''
    homeassistant_url = 'ws://127.0.0.1:8123/api/websocket'
    include_by_default = False
    _running = False
    latest_states.clear()
    discovery_result.clear()
    with _state_lock:
        _status = _blank_status()
    statusRegistry.replace("homeassistant", status())


def model_id_for(supported_colour_modes, entity_id=None):
    """Map Home Assistant colour modes onto the closest Hue model id.

    The original expression relied on ``or``/``and`` precedence and therefore
    only applied the colour-temperature check to the ``rgbww`` term.
    """
    modes = set(supported_colour_modes or [])
    if modes & set(COLOUR_MODES):
        return "LCT015"
    if COLOR_TEMP in modes:
        return "LTW001"
    if BRIGHTNESS in modes:
        return "LWB010"
    if ONOFF in modes:
        return "LOM001"
    # switch.* entities carry no colour modes at all, so they used to be
    # dropped even though the include filter and the service call both
    # support them. They are on/off by definition.
    if isinstance(entity_id, str) and entity_id.startswith("switch."):
        return "LOM001"
    return None


def _is_supported_entity(ha_state):
    entity_id = (ha_state or {}).get("entity_id")
    return isinstance(entity_id, str) and entity_id.startswith(SUPPORTED_DOMAINS)


def _diyhue_flag(ha_state):
    return (ha_state or {}).get("attributes", {}).get("diyhue")


def _should_include(ha_state, include_by_default=None):
    """Whether an entity should be exposed as a Hue light.

    Without ``homeAssistantIncludeByDefault`` an entity has to opt in by
    carrying a ``diyhue: include`` attribute - which is why a stock Home
    Assistant yields exactly zero lights.
    """
    if include_by_default is None:
        include_by_default = globals()["include_by_default"]
    if not _is_supported_entity(ha_state):
        return False
    flag = _diyhue_flag(ha_state)
    if include_by_default:
        return flag != "exclude"
    return flag == "include"


def record_states(ha_states):
    """Replace the entity cache from a ``get_states`` result and count why.

    The counts are what the status page uses to explain an empty discovery.
    """
    seen = 0
    tagged = 0
    excluded = []
    included = {}
    for ha_state in ha_states or []:
        if not _is_supported_entity(ha_state):
            continue
        seen += 1
        if _diyhue_flag(ha_state) is not None:
            tagged += 1
        if _should_include(ha_state):
            included[ha_state["entity_id"]] = ha_state
        elif len(excluded) < EXCLUDED_SAMPLE_SIZE:
            excluded.append(ha_state["entity_id"])

    latest_states.clear()
    latest_states.update(included)
    for entity_id in included:
        logging.info(f"Found {entity_id}")
    logging.info("Home Assistant states: %s supported entities, %s included, %s tagged",
                 seen, len(included), tagged)
    _set_discovery_status(
        include_by_default=include_by_default,
        entities_seen=seen,
        entities_included=len(included),
        entities_tagged=tagged,
        excluded_sample=excluded,
        last_discovery=_now(),
    )
    return len(included)


def handle_result_message(message, message_type):
    """Process a ``result`` frame.

    An unsuccessful or empty result has to release ``discovery_result`` too,
    otherwise discovery blocks for the full timeout with nothing to show.
    """
    if message_type != "getstates":
        return
    if message.get("success") is False:
        error = message.get("error", {})
        reason = error.get("message") or error.get("code") or "unknown error"
        logging.error("Home Assistant rejected get_states: %s", reason)
        _set_status(last_error=f"get_states failed: {reason}")
    else:
        record_states(message.get("result") or [])
    discovery_result.set()


class HomeAssistantClient(WebSocketClient):

    message_id = 1
    id_to_type = {}

    def opened(self):
        logging.info("Home Assistant WebSocket Connection Opened")
        _set_status(connected=True, last_error=None)

    def closed(self, code, reason=None):
        logging.info(
            "Home Assistant WebSocket Connection Closed. Code: {} Reason {}".format(code, reason))
        for home_assistant_state in latest_states.values():
            if 'state' in home_assistant_state:
                home_assistant_state['state'] = 'unavailable'
        _set_status(connected=False, authenticated=False)
        # Let the supervisor reconnect immediately rather than after a sleep.
        _wake_supervisor.set()

    def received_message(self, m):
        # logging.debug("Received message: {}".format(m))
        message_text = m.data.decode(m.encoding)
        message = json.loads(message_text)
        if message.get('type', None) == "auth_required":
            self.do_auth_required(message)
        elif message.get('type', None) == "auth_ok":
            self.do_auth_complete()
        elif message.get('type', None) == "auth_invalid":
            self.do_auth_invalid(message)
        elif message.get('type', None) == "result":
            self.do_result(message)
        elif message.get('type', None) == "event":
            self.do_event(message)
        elif message.get('type', None) == "pong":
            self.do_pong(message)
        else:
            logging.warning("Unexpected message: %s", message)

    def do_pong(self, message):
        logging.debug("Home Assistant pong: %s", message)

    def do_auth_required(self, m):
        logging.info("Home Assistant Web Socket Authorisation required")
        payload = {
            'type': 'auth',
            'access_token': homeassistant_token
        }
        self._send(payload)

    def do_auth_invalid(self, message):
        reason = message.get("message", "access token rejected")
        logging.error(
            "Home Assistant Web Socket Authorisation invalid: {}".format(message))
        _set_status(authenticated=False, last_error=f"authentication failed: {reason}")
        # A bad token will never fix itself by retrying; release any waiter.
        discovery_result.set()

    def do_auth_complete(self):
        logging.info("Home Assistant Web Socket Authorisation complete")
        _set_status(authenticated=True, last_error=None)
        self.get_all_lights()
        self.subscribe_for_updates()

    def get_all_lights(self):
        discovery_result.clear()
        payload = {
            'type': 'get_states'
        }
        self._send_with_id(payload, "getstates")

    def subscribe_for_updates(self):
        payload = {
            "type": "subscribe_events",
            "event_type": "state_changed"
        }
        self._send_with_id(payload, "subscribe")

    def change_light(self, light, data):
        service_data = {}
        entity_id = light.protocol_cfg['entity_id']
        service_data['entity_id'] = entity_id
        domain = entity_id.split(".", 1)[0]
        if domain not in ("light", "switch"):
            raise HomeAssistantUnavailable(
                f"unsupported Home Assistant domain for {entity_id}")
        payload = {
            "type": "call_service",
            "domain": domain,
            "service_data": service_data
        }
        payload["service"] = "turn_on"
        if 'on' in data:
            if not data['on']:
                payload["service"] = "turn_off"

        color_from_hsv = False
        for key, value in data.items():
            if key == "ct":
                service_data['color_temp_kelvin'] = int(round(1000000 / value))
            if key == "bri":
                service_data['brightness'] = value
            if key == "xy":
                service_data['xy_color'] = [value[0], value[1]]
            if key == "hue":
                color_from_hsv = True
            if key == "sat":
                color_from_hsv = True
            if key == "on":
                if value:
                    payload["service"] = "turn_on"
                else:
                    payload["service"] = "turn_off"
            if key == "alert":
                service_data['flash'] = "long"
            if key == "transitiontime":
                service_data['transition'] = value / 10

        if color_from_hsv:
            # Hue uses hue 0-65535 / saturation 0-254, Home Assistant expects
            # degrees and percent.
            service_data['hs_color'] = [
                round(data.get('hue', 0) / 65535 * 360, 3),
                round(data.get('sat', 0) / 254 * 100, 3),
            ]

        self._send_with_id(payload, "service")

    def do_result(self, message):
        message_type = self.id_to_type.pop(message.get('id'), None)
        handle_result_message(message, message_type)

    def do_event(self, message):
        try:
            event_type = message['event']['event_type']
            if event_type == 'state_changed':
                self.do_state_changed(message)
        except KeyError:
            logging.exception("No event_type  in event")

    def do_state_changed(self, message):
        try:
            entity_id = message['event']['data']['entity_id']
            new_state = message['event']['data']['new_state']
            if _should_include(new_state):
                logging.debug("State update recevied for {}, new state {}".format(
                    entity_id, new_state))
                latest_states[entity_id] = new_state
        except KeyError:
            logging.exception("No state in event: %s", message)

    def _send_with_id(self, payload, type_of_call):
        payload['id'] = self.message_id
        self.id_to_type[self.message_id] = type_of_call
        self.message_id += 1
        self._send(payload)

    def _send(self, payload):
        json_payload = json.dumps(payload)
        self.send(json_payload)


def _client_is_usable(client):
    return client is not None and not client.client_terminated


def connect_if_required():
    """Return a usable client, connecting first if necessary.

    Raises ``HomeAssistantUnavailable`` rather than handing back ``None``; every
    caller previously dereferenced the result unconditionally.
    """
    if not _client_is_usable(homeassistant_ws_client):
        create_websocket_client()
    if not _client_is_usable(homeassistant_ws_client):
        raise HomeAssistantUnavailable(
            status().get("last_error") or f"could not connect to {homeassistant_url}")
    return homeassistant_ws_client


def create_websocket_client():
    global homeassistant_ws_client

    _set_status(last_connect_attempt=_now())
    try:
        client = HomeAssistantClient(homeassistant_url, protocols=['http-only', 'chat'])
        client.connect()
        homeassistant_ws_client = client
        logging.info("Home Assistant Web Socket Client connected to %s", homeassistant_url)
        _set_status(connected=True, last_error=None)
    except Exception as e:
        homeassistant_ws_client = None
        message = f"{type(e).__name__}: {e}"
        logging.warning("Error connecting to Home Assistant WebSocket at %s - %s",
                        homeassistant_url, message)
        _set_status(connected=False, authenticated=False, last_error=message)
    return homeassistant_ws_client


def configure(ha_config):
    """Apply the ``config.homeassistant`` section without connecting.

    Every key is optional: an upgraded config.yaml only ever gains
    ``{"enabled": False}``, so each lookup needs a default rather than the
    conditional assignment that used to raise ``UnboundLocalError``.
    """
    global homeassistant_token, homeassistant_url, include_by_default

    ha_config = ha_config or {}
    homeassistant_ip = ha_config.get('homeAssistantIp', '127.0.0.1')
    homeassistant_port = ha_config.get('homeAssistantPort', 8123)
    homeassistant_token = ha_config.get('homeAssistantToken', '')
    include_by_default = bool(ha_config.get('homeAssistantIncludeByDefault', False))
    use_https = bool(ha_config.get('homeAssistantUseHttps', False))

    ws_prefix = "wss" if use_https else "ws"
    homeassistant_url = f'{ws_prefix}://{homeassistant_ip}:{homeassistant_port}/api/websocket'

    _set_status(enabled=bool(ha_config.get('enabled', False)),
                url=homeassistant_url,
                token_configured=bool(homeassistant_token))
    _set_discovery_status(include_by_default=include_by_default)
    return homeassistant_url


def create_ws_client(bridgeConfig):
    """Backwards compatible entry point: configure from the bridge config and connect."""
    configure(bridgeConfig["config"]["homeassistant"])
    return create_websocket_client()


def is_running():
    """Whether the reconnect supervisor is active."""
    return _running and _supervisor is not None and _supervisor.is_alive()


def _supervise():
    """Keep the websocket connected for as long as the integration is enabled."""
    delay = RECONNECT_INITIAL_DELAY
    while _running:
        if _client_is_usable(homeassistant_ws_client):
            delay = RECONNECT_INITIAL_DELAY
        else:
            create_websocket_client()
            if _client_is_usable(homeassistant_ws_client):
                delay = RECONNECT_INITIAL_DELAY
            else:
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
        _wake_supervisor.wait(timeout=delay)
        _wake_supervisor.clear()


def start(ha_config):
    """Configure and connect. Safe to call repeatedly."""
    global _supervisor, _running

    configure(ha_config)
    stop(keep_config=True)
    _running = True
    _wake_supervisor.clear()
    _supervisor = threading.Thread(target=_supervise, name="homeassistant-ws",
                                   daemon=True)
    _supervisor.start()
    return status()


def stop(keep_config=False):
    """Close the websocket and stop reconnecting."""
    global _supervisor, _running, homeassistant_ws_client

    _running = False
    _wake_supervisor.set()
    supervisor, _supervisor = _supervisor, None
    if supervisor is not None and supervisor.is_alive():
        supervisor.join(timeout=RECONNECT_INITIAL_DELAY + 1)

    client, homeassistant_ws_client = homeassistant_ws_client, None
    if client is not None:
        try:
            client.close()
        except Exception:
            logging.debug("Error closing Home Assistant websocket", exc_info=True)
    latest_states.clear()
    _set_status(connected=False, authenticated=False,
                enabled=status()["enabled"] if keep_config else False)


def request_states():
    """Ask Home Assistant for a fresh entity list and wait for the answer."""
    client = connect_if_required()
    discovery_result.clear()
    client.get_all_lights()
    completed = discovery_result.wait(timeout=discovery_timeout_seconds)
    if not completed:
        message = f"timed out after {discovery_timeout_seconds}s waiting for get_states"
        _set_status(last_error=message)
        raise HomeAssistantUnavailable(message)
    return status()


def test_connection(ha_config=None):
    """Connect, fetch states and report the counts, without creating lights."""
    if ha_config is not None:
        configure(ha_config)
        create_websocket_client()
    try:
        request_states()
    except HomeAssistantUnavailable as e:
        return {"ok": False, "error": str(e), "status": status()}
    current = status()
    return {"ok": not current["last_error"], "error": current["last_error"], "status": current}


def discover(detectedLights):
    logging.info("HomeAssistant WebSocket discovery called")
    request_states()
    logging.info("HomeAssistant WebSocket discovery devices received")
    # This only loops over discovered devices so we have already filtered out what we don't want
    for entity_id, ha_state in list(latest_states.items()):
        attributes = ha_state.get("attributes", {})
        lightName = attributes.get("friendly_name", entity_id)

        model_id = model_id_for(attributes.get('supported_color_modes', []), entity_id)
        if model_id is None:
            logging.info("unknown model id " + str(attributes.get('supported_color_modes')))
            continue

        logging.info("HomeAssistant_ws: found light {}".format(lightName))
        protocol_cfg = {"entity_id": entity_id,
                        "ip": "none"}

        detectedLights.append({"protocol": "homeassistant_ws", "name": lightName,
                               "modelid": model_id, "protocol_cfg": protocol_cfg})

    logging.info("HomeAssistant WebSocket discovery complete")


statusRegistry.replace("homeassistant", status())
