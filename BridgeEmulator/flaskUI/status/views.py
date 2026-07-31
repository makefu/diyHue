"""Status and diagnostics page.

The bundled single-page app is built from a separate repository and ships as a
minified release archive, so this page is deliberately self-contained: a Jinja
template plus plain JavaScript, served from the assets directory that already
exists. It answers the questions the main interface cannot: is a scan running,
which integration failed, and why did Home Assistant return no lights.
"""

import json
import queue
from threading import Thread

import flask_login
from flask import Blueprint, jsonify, render_template, request
from flask_sock import Sock

import configManager
import logManager
from lights import discover
from services import homeAssistantWS, serviceManager, statusRegistry

logging = logManager.logger.get_logger(__name__)
bridgeConfig = configManager.bridgeConfig.yaml_config

status = Blueprint('status', __name__, url_prefix='/status')
sock = Sock()

# How long the websocket waits for an event before sending a keepalive. Proxies
# routinely drop idle upstream connections after 60s.
WEBSOCKET_PING_INTERVAL = 25
LOG_TAIL_LINES = 200

# Config keys the Home Assistant card is allowed to write.
HOME_ASSISTANT_SETTINGS = {
    "homeAssistantIp": str,
    "homeAssistantPort": int,
    "homeAssistantToken": str,
    "homeAssistantIncludeByDefault": bool,
    "homeAssistantUseHttps": bool,
}


def _protocol_states():
    """Enabled state and last scan outcome for every discovery protocol."""
    scan = statusRegistry.get("scan")
    last_run = scan.get("protocols") or {}
    states = {}
    for protocol in discover.PROTOCOLS:
        try:
            enabled = protocol.is_enabled(bridgeConfig["config"])
        except Exception as e:
            logging.debug("Cannot evaluate %s: %s", protocol.name, e)
            enabled = False
        states[protocol.name] = {
            "enabled": enabled,
            "managed": protocol.name in serviceManager.SERVICES,
            "toggleable": protocol.toggleable,
            "last_run": last_run.get(protocol.name, {}),
        }
    return states


def _build_state(log_level="INFO"):
    snapshot = statusRegistry.snapshot()
    components = snapshot["components"]
    return {
        "bridge": {
            "name": bridgeConfig["config"].get("name"),
            "ipaddress": bridgeConfig["config"].get("ipaddress"),
            "swversion": bridgeConfig["config"].get("swversion"),
            "loglevel": logManager.logger.get_level_name(),
            "lights": len(bridgeConfig["lights"]),
        },
        "services": serviceManager.status(),
        "protocols": _protocol_states(),
        "scan": components.get("scan", {"state": "idle"}),
        "homeassistant": homeAssistantWS.status(),
        "log": logManager.logger.get_recent(limit=LOG_TAIL_LINES, level=log_level),
        "seq": snapshot["seq"],
    }


@status.route('/')
@flask_login.login_required
def index():
    return render_template('status.html')


@status.route('/api/state')
@flask_login.login_required
def state():
    level = request.args.get('level', default='INFO', type=str).upper()
    return jsonify(_build_state(log_level=level))


@status.route('/api/log')
@flask_login.login_required
def log():
    limit = request.args.get('limit', default=LOG_TAIL_LINES, type=int)
    level = request.args.get('level', default='INFO', type=str).upper()
    return jsonify(logManager.logger.get_recent(limit=limit, level=level))


@status.route('/api/service/<string:name>', methods=['POST'])
@flask_login.login_required
def set_service(name):
    """Enable or disable an integration and make it take effect immediately."""
    payload = request.get_json(force=True, silent=True) or {}
    if "enabled" not in payload:
        return jsonify({"error": "missing 'enabled'"}), 400
    enabled = bool(payload["enabled"])

    protocols = {protocol.name: protocol for protocol in discover.PROTOCOLS}
    if name in serviceManager.SERVICES:
        result = serviceManager.set_enabled(bridgeConfig, name, enabled)
        running = result["running"]
    elif name in protocols:
        if not protocols[name].toggleable:
            return jsonify({
                "error": f"{name} is switched on by pairing it, not by a flag"
            }), 400
        # Discovery-only protocols re-read their flag on every scan, so writing
        # the config is all that is needed.
        bridgeConfig["config"].setdefault(name, {})["enabled"] = enabled
        running = enabled
    else:
        return jsonify({"error": f"unknown integration {name}"}), 404

    configManager.bridgeConfig.save_config(backup=False, resource="config")
    statusRegistry.event("service_state", service=name, enabled=enabled, running=running)
    return jsonify(_build_state())


@status.route('/api/homeassistant', methods=['POST'])
@flask_login.login_required
def set_home_assistant():
    """Update the Home Assistant connection settings and reconnect."""
    payload = request.get_json(force=True, silent=True) or {}
    section = bridgeConfig["config"].setdefault("homeassistant", {})
    for key, caster in HOME_ASSISTANT_SETTINGS.items():
        if key not in payload:
            continue
        try:
            section[key] = caster(payload[key]) if caster is not bool else bool(payload[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"invalid value for {key}"}), 400
    if "enabled" in payload:
        section["enabled"] = bool(payload["enabled"])

    configManager.bridgeConfig.save_config(backup=False, resource="config")
    serviceManager.apply(bridgeConfig, changed_keys={"homeassistant"})
    return jsonify(_build_state())


@status.route('/api/homeassistant/test', methods=['POST'])
@flask_login.login_required
def test_home_assistant():
    """Connect and fetch the entity list without creating any lights."""
    result = homeAssistantWS.test_connection(bridgeConfig["config"].get("homeassistant"))
    statusRegistry.event("ha_test", ok=result["ok"], error=result["error"])
    return jsonify(result)


@status.route('/api/scan', methods=['POST'])
@flask_login.login_required
def scan():
    if statusRegistry.get("scan").get("state") == "active":
        return jsonify({"error": "a scan is already running"}), 409
    Thread(target=discover.scanForLights, name="light-scan").start()
    return jsonify({"started": True})


@sock.route('/ws', bp=status)
def stream_status(ws):
    """Push the snapshot, then every status event as it happens."""
    sink = statusRegistry.subscribe()
    if sink is None:
        ws.send(json.dumps({"kind": "error", "error": "too many status subscribers"}))
        return
    try:
        ws.send(json.dumps({"kind": "snapshot", "state": _build_state()}))
        while True:
            try:
                event = sink.get(timeout=WEBSOCKET_PING_INTERVAL)
            except queue.Empty:
                ws.send(json.dumps({"kind": "ping"}))
                continue
            ws.send(json.dumps(event))
    finally:
        statusRegistry.unsubscribe(sink)
