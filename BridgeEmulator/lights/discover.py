import logManager
import configManager
import socket
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Tuple, Union, Generator
from lights.protocols import tpkasa, wled, mqtt, hyperion, yeelight, hue, deconz, native_multi, tasmota, shelly, esphome, tradfri, elgato, govee
from services import homeAssistantWS, statusRegistry
from HueObjects import Light, StreamEvent
from functions.core import nextFreeId
from lights.light_types import lightTypes

logging = logManager.logger.get_logger(__name__)
bridgeConfig = configManager.bridgeConfig.yaml_config

# A serial connect() sweep costs ~5s per /24 per port. The timeout dominates, so
# the work is almost entirely idle wait and parallelises cleanly.
SWEEP_WORKERS = 64
# How often the sweep reports back, in hosts.
SWEEP_PROGRESS_INTERVAL = 16

def pretty_json(data: Union[Dict, List]) -> str:
    """
    Convert a dictionary or list to a pretty-printed JSON string.

    Args:
        data (Union[Dict, List]): The data to convert.

    Returns:
        str: The pretty-printed JSON string.
    """
    return json.dumps(data, sort_keys=True, indent=4, separators=(',', ': '))

def scanHost(host: str, port: int) -> int:
    """
    Scan a host to check if a port is open.

    Args:
        host (str): The host to scan.
        port (int): The port to check.

    Returns:
        int: The result of the connection attempt (0 if successful).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.02)
        return sock.connect_ex((host, port))

def iter_ips(port: int) -> Generator[Tuple[str, int], None, None]:
    """
    Generate IP addresses within the configured range.

    Args:
        port (int): The port to check.

    Yields:
        Generator[Tuple[str, int], None, None]: A tuple of host and port.
    """
    rangeConfig = bridgeConfig["config"]["IP_RANGE"]
    HOST_IP = configManager.runtimeConfig.arg["HOST_IP"]
    scan_on_host_ip = bridgeConfig["config"]["scanonhostip"]
    ip_range_start = rangeConfig["IP_RANGE_START"]
    ip_range_end = rangeConfig["IP_RANGE_END"]
    sub_ip_range_start = rangeConfig["SUB_IP_RANGE_START"]
    sub_ip_range_end = rangeConfig["SUB_IP_RANGE_END"]
    host = HOST_IP.split('.')
    if scan_on_host_ip:
        yield ('127.0.0.1', port)
    for sub_addr in range(sub_ip_range_start, sub_ip_range_end + 1):
        host[2] = str(sub_addr)
        for addr in range(ip_range_start, ip_range_end + 1):
            host[3] = str(addr)
            test_host = '.'.join(host)
            if test_host != HOST_IP:
                yield (test_host, port)

def find_hosts(port: int) -> List[str]:
    """
    Find hosts with the specified port open.

    The sweep runs in a thread pool and reports progress to the status registry
    so the web interface can show how far along it is.

    Args:
        port (int): The port to check.

    Returns:
        List[str]: A list of hosts with the port open.
    """
    targets = list(iter_ips(port))
    total = len(targets)
    statusRegistry.update("scan", sweep={"port": port, "scanned": 0, "total": total})

    found = []
    scanned = 0
    with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
        for (host, host_port), result in zip(targets, pool.map(lambda t: scanHost(*t), targets)):
            scanned += 1
            if result == 0:
                found.append(f'{host}:{host_port}')
            if scanned % SWEEP_PROGRESS_INTERVAL == 0 or scanned == total:
                statusRegistry.update("scan", sweep={"port": port, "scanned": scanned, "total": total})
                statusRegistry.event("scan_progress", port=port, scanned=scanned,
                                     total=total, found=len(found))
    return found

def addNewLight(modelid: str, name: str, protocol: str, protocol_cfg: Dict) -> Union[int, bool]:
    """
    Add a new light to the bridge configuration.

    Args:
        modelid (str): The model ID of the light.
        name (str): The name of the light.
        protocol (str): The protocol used by the light.
        protocol_cfg (Dict): The protocol configuration.

    Returns:
        Union[int, bool]: The ID of the new light or False if the model ID is not found.
    """
    newLightID = nextFreeId(bridgeConfig, "lights")
    if modelid in lightTypes:
        light = lightTypes[modelid]
        light.update({
            "name": name,
            "id_v1": newLightID,
            "modelid": modelid,
            "protocol": protocol,
            "protocol_cfg": protocol_cfg
        })
        newObject = Light.Light(light)
        bridgeConfig["lights"][newLightID] = newObject
        bridgeConfig["groups"]["0"].add_light(newObject)
        rooms = [obj.id_v2 for obj in bridgeConfig["groups"].values()]
        lights = [obj.id_v2 for obj in bridgeConfig["lights"].values()]
        bridgeConfig["groups"]["0"].groupZeroStream(rooms, lights)
        configManager.bridgeConfig.save_config(backup=False, resource="lights")
        return newLightID
    return False

def manualAddLight(ip: str, protocol: str, config: Dict = {}) -> None:
    """
    Manually add a light by IP address.

    Args:
        ip (str): The IP address of the light.
        protocol (str): The protocol used by the light.
        config (Dict, optional): Additional configuration for the light. Defaults to {}.
    """
    modelid = config.get("lightModelID", "LCT015")
    name = config.get("lightName", "New Light")
    if protocol == "auto":
        detectedLights = []
        for discover_func in [native_multi.discover, tasmota.discover, shelly.discover, esphome.discover]:
            discover_func(detectedLights, [ip])
        for light in detectedLights:
            logging.info(f"Found light {light['protocol']} {light['name']}")
            addNewLight(light["modelid"], light["name"], light["protocol"], light["protocol_cfg"])
    else:
        config["ip"] = ip
        addNewLight(modelid, name, protocol, config)

def discoveryEvent() -> None:
    """
    Trigger a discovery event for Zigbee devices.
    """
    streamMessage = {
        "creationtime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": [{
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, bridgeConfig["config"]["bridgeid"] + 'zigbee_device_discovery')),
            "owner": {
                "rid": str(uuid.uuid5(uuid.NAMESPACE_URL, bridgeConfig["config"]["bridgeid"] + 'device')),
                "rtype": "device"
            },
            "status": bridgeConfig["config"]["zigbee_device_discovery_info"]["status"],
            "type": "zigbee_device_discovery"
        }],
        "id": str(uuid.uuid4()),
        "type": "update"
    }
    StreamEvent(streamMessage)

def update_light_ip(lightObj: Light.Light, light: Dict) -> None:
    """
    Update the IP address of a light.

    Args:
        lightObj (Light.Light): The light object to update.
        light (Dict): The new light data.
    """
    if "ip" in light["protocol_cfg"]:
        lightObj.protocol_cfg["ip"] = light["protocol_cfg"]["ip"]
    if light["protocol"] == "wled":
        lightObj.protocol_cfg.update({
            "ledCount": light["protocol_cfg"]["ledCount"],
            "segment_start": light["protocol_cfg"]["segment_start"],
            "udp_port": light["protocol_cfg"]["udp_port"]
        })
    if light["protocol"] == "govee":
        lightObj.protocol_cfg.update({
            "bri_range": light["protocol_cfg"]["bri_range"]
        })
    logging.info(f"Update IP/config for light {light['name']}")

def is_light_matching(lightObj: Light.Light, light: Dict) -> bool:
    """
    Check if a light matches an existing light object.

    Args:
        lightObj (Light.Light): The existing light object.
        light (Dict): The new light data.

    Returns:
        bool: True if the light matches, False otherwise.
    """
    protocol = light["protocol"]
    if protocol == "native_multi":
        return (lightObj.protocol_cfg["mac"] == light["protocol_cfg"]["mac"] and
                lightObj.protocol_cfg["light_nr"] == light["protocol_cfg"]["light_nr"] and
                lightObj.modelid == light["modelid"])
    if protocol in ["yeelight", "tasmota", "tradfri", "hyperion", "tpkasa"]:
        return lightObj.protocol_cfg["id"] == light["protocol_cfg"]["id"] and lightObj.modelid == light["modelid"]
    if protocol in ["shelly", "native", "native_single", "esphome", "elgato"]:
        return lightObj.protocol_cfg["mac"] == light["protocol_cfg"]["mac"] and lightObj.modelid == light["modelid"]
    if protocol in ["hue", "deconz"]:
        return lightObj.protocol_cfg["uniqueid"] == light["protocol_cfg"]["uniqueid"] and lightObj.modelid == light["modelid"]
    if protocol == "wled":
        return (lightObj.protocol_cfg["mac"] == light["protocol_cfg"]["mac"] and
                lightObj.protocol_cfg["segmentId"] == light["protocol_cfg"]["segmentId"] and
                lightObj.modelid == light["modelid"])
    if protocol == "homeassistant_ws":
        return lightObj.protocol_cfg["entity_id"] == light["protocol_cfg"]["entity_id"] and lightObj.modelid == light["modelid"]
    if protocol == "govee":
        return (lightObj.protocol_cfg["device_id"] == light["protocol_cfg"]["device_id"] and
                lightObj.protocol_cfg["sku_model"] == light["protocol_cfg"]["sku_model"] and
                lightObj.protocol_cfg.get("segmentedID", -1) == light["protocol_cfg"].get("segmentedID", -1))
    return False

def get_device_ips() -> List[str]:
    """
    Get the IP addresses of devices to scan.

    Returns:
        List[str]: A list of device IP addresses.
    """
    if bridgeConfig["config"]["port"]["enabled"]:
        return [host for ports in bridgeConfig["config"]["port"]["ports"] for host in find_hosts(ports)]
    return find_hosts(80)

@dataclass
class DiscoveryProtocol:
    """One entry in the discovery sequence.

    Wrapping every protocol in the same structure means the enable check, the
    error handling and the progress reporting are written once instead of
    fifteen times - and one protocol raising can no longer abort the scan.
    """
    name: str
    run: Callable[[List[Dict], List[str]], None]
    is_enabled: Callable[[Dict], bool]
    # False for protocols that are switched on by pairing rather than by a
    # flag, so the web interface can say so instead of offering a checkbox
    # that would leave them enabled but unusable.
    toggleable: bool = True


def _enabled_flag(key: str) -> Callable[[Dict], bool]:
    return lambda config: bool(config.get(key, {}).get("enabled", False))


def _configured(key: str) -> Callable[[Dict], bool]:
    """hue and tradfri have no `enabled` flag; they count as on once set up."""
    return lambda config: bool(config.get(key))


def _elgato_discover(detectedLights: List[Dict], device_ips: List[str]) -> None:
    # Scan with port 9123 before mDNS discovery
    elgato_ips = find_hosts(9123)
    logging.info(pretty_json(elgato_ips))
    elgato.discover(detectedLights, elgato_ips)


PROTOCOLS = [
    # broadcast MQTT message, lights will be added by the service
    DiscoveryProtocol("mqtt", lambda detected, ips: mqtt.discover(bridgeConfig["config"]["mqtt"]),
                      _enabled_flag("mqtt")),
    DiscoveryProtocol("deconz", lambda detected, ips: deconz.discover(detected, bridgeConfig["config"]["deconz"]),
                      _enabled_flag("deconz")),
    DiscoveryProtocol("homeassistant", lambda detected, ips: homeAssistantWS.discover(detected),
                      _enabled_flag("homeassistant")),
    DiscoveryProtocol("yeelight", lambda detected, ips: yeelight.discover(detected),
                      _enabled_flag("yeelight")),
    # native_multi probe all esp8266 lights with firmware from diyhue repo
    DiscoveryProtocol("native_multi", lambda detected, ips: native_multi.discover(detected, ips),
                      _enabled_flag("native_multi")),
    DiscoveryProtocol("tasmota", lambda detected, ips: tasmota.discover(detected, ips),
                      _enabled_flag("tasmota")),
    # Most of the other discoveries are disabled by having no IP address
    # (--disable-network-scan) but wled does an mdns discovery as well.
    DiscoveryProtocol("wled", lambda detected, ips: wled.discover(detected, ips),
                      _enabled_flag("wled")),
    DiscoveryProtocol("hue", lambda detected, ips: hue.discover(detected, bridgeConfig["config"]["hue"]),
                      _configured("hue"), toggleable=False),
    DiscoveryProtocol("shelly", lambda detected, ips: shelly.discover(detected, ips),
                      _enabled_flag("shelly")),
    DiscoveryProtocol("esphome", lambda detected, ips: esphome.discover(detected, ips),
                      _enabled_flag("esphome")),
    DiscoveryProtocol("tradfri", lambda detected, ips: tradfri.discover(detected, bridgeConfig["config"]["tradfri"]),
                      _configured("tradfri"), toggleable=False),
    DiscoveryProtocol("hyperion", lambda detected, ips: hyperion.discover(detected),
                      _enabled_flag("hyperion")),
    DiscoveryProtocol("tpkasa", lambda detected, ips: tpkasa.discover(detected),
                      _enabled_flag("tpkasa")),
    DiscoveryProtocol("elgato", _elgato_discover, _enabled_flag("elgato")),
    DiscoveryProtocol("govee", lambda detected, ips: govee.discover(detected),
                      _enabled_flag("govee")),
]


def discover_lights(detectedLights: List[Dict], device_ips: List[str]) -> Dict:
    """
    Discover lights on the network.

    Each protocol is isolated: a protocol that raises is recorded and the scan
    carries on with the next one.

    Args:
        detectedLights (List[Dict]): A list to store detected lights.
        device_ips (List[str]): A list of device IP addresses to scan.

    Returns:
        Dict: Per-protocol outcome, keyed by protocol name.
    """
    results = {}
    for protocol in PROTOCOLS:
        try:
            enabled = protocol.is_enabled(bridgeConfig["config"])
        except Exception as e:
            logging.exception(f"Cannot evaluate whether {protocol.name} is enabled")
            results[protocol.name] = {"state": "error", "error": f"{type(e).__name__}: {e}"}
            continue
        if not enabled:
            results[protocol.name] = {"state": "disabled"}
            continue

        before = len(detectedLights)
        results[protocol.name] = {"state": "running"}
        statusRegistry.update("scan", protocols=dict(results))
        statusRegistry.event("protocol_started", protocol=protocol.name)
        try:
            protocol.run(detectedLights, device_ips)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            logging.exception(f"Discovery failed for {protocol.name}")
            results[protocol.name] = {"state": "error", "error": message}
            statusRegistry.event("protocol_error", protocol=protocol.name, error=message)
        else:
            found = len(detectedLights) - before
            results[protocol.name] = {"state": "ok", "found": found}
            statusRegistry.event("protocol_ok", protocol=protocol.name, found=found)
        statusRegistry.update("scan", protocols=dict(results))
    return results

def scanForLights() -> Dict:  # scan for ESP8266 lights and strips
    """
    Scan for ESP8266 lights and strips.

    Returns:
        Dict: The scan result.
    """
    logging.info("scan for light")
    bridgeConfig["temp"]["scanResult"] = {"lastscan": "active"}
    bridgeConfig["config"]["zigbee_device_discovery_info"]["status"] = "active"
    discoveryEvent()
    statusRegistry.update("scan", state="active", started=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                          protocols={}, sweep=None, found=[], error=None)
    statusRegistry.event("scan_started")
    detectedLights = []
    newLights = []
    try:
        device_ips = get_device_ips()
        logging.info(f"Scanning for lights on\n{pretty_json(device_ips)}")
        discover_lights(detectedLights, device_ips)
        newLights = _register_detected_lights(detectedLights)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        logging.exception("Light scan failed")
        statusRegistry.update("scan", error=message)
    finally:
        # These drive the "searching" indicator in both web interfaces; leaving
        # them on "active" makes a failed scan look like it never finished.
        bridgeConfig["temp"]["scanResult"]["lastscan"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        bridgeConfig["config"]["zigbee_device_discovery_info"]["status"] = "ready"
        discoveryEvent()
        statusRegistry.update("scan", state="idle",
                              lastscan=bridgeConfig["temp"]["scanResult"]["lastscan"],
                              found=newLights)
        statusRegistry.event("scan_finished", found=len(newLights),
                             detected=len(detectedLights))
    return bridgeConfig["temp"]["scanResult"]


def _register_detected_lights(detectedLights: List[Dict]) -> List[Dict]:
    """Add newly detected lights to the bridge, refreshing the ones we know."""
    newLights = []
    for light in detectedLights:
        lightIsNew = True
        for lightObj in bridgeConfig["lights"].values():
            if lightObj.protocol == light["protocol"] and is_light_matching(lightObj, light):
                update_light_ip(lightObj, light)
                lightIsNew = False
                break
        if not lightIsNew:
            continue
        logging.info(f"Add new light {light['name']}")
        lightId = addNewLight(light["modelid"], light["name"], light["protocol"], light["protocol_cfg"])
        if not lightId:
            # addNewLight returns False for a model id we have no template for;
            # storing that would put a `False` key into the scan result.
            logging.warning(f"Unknown model id {light['modelid']} for {light['name']}, ignoring")
            statusRegistry.event("light_error", name=light["name"], protocol=light["protocol"],
                                 error=f"unknown model id {light['modelid']}")
            continue
        bridgeConfig["temp"]["scanResult"][lightId] = {"name": light["name"]}
        entry = {"id": lightId, "name": light["name"], "protocol": light["protocol"],
                 "modelid": light["modelid"]}
        newLights.append(entry)
        statusRegistry.event("light_found", **entry)
    return newLights
