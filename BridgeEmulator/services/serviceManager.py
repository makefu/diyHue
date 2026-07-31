"""Lifecycle for the integrations that own a long-lived connection.

Home Assistant, MQTT and deConz used to be started exactly once, from ``main()``
at boot. Toggling them in the web interface wrote the config and did nothing
else, so the change only took effect after a restart - and, for Home Assistant,
a later enable connected to the module default address with an empty token.

``apply()`` reconciles the running services with the configuration and is safe
to call from both startup and the config PUT handler.
"""

import logManager
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from services import statusRegistry

logging = logManager.logger.get_logger(__name__)

# A stuck integration must never block the HTTP request that disabled it.
STOP_TIMEOUT_SECONDS = 5

_lock = threading.RLock()
_threads: Dict[str, threading.Thread] = {}
_errors: Dict[str, Optional[str]] = {}


@dataclass
class ManagedService:
    """A service whose connection outlives a single request.

    ``start`` normally blocks and is therefore run on its own thread. Services
    that manage their own threads set ``threaded=False`` and provide
    ``running`` so the manager can still tell whether they are up.
    """
    name: str
    config_key: str
    start: Callable[[Dict], None]
    stop: Callable[[], None]
    threaded: bool = True
    running: Optional[Callable[[], bool]] = None


def _homeassistant():
    from services import homeAssistantWS
    return homeAssistantWS


def _mqtt():
    from services import mqtt
    return mqtt


def _deconz():
    from services import deconz
    return deconz


SERVICES = {
    "homeassistant": ManagedService(
        name="homeassistant",
        config_key="homeassistant",
        start=lambda config: _homeassistant().start(config),
        stop=lambda: _homeassistant().stop(),
        # homeAssistantWS runs its own reconnect supervisor thread.
        threaded=False,
        running=lambda: _homeassistant().is_running(),
    ),
    "mqtt": ManagedService(
        name="mqtt",
        config_key="mqtt",
        start=lambda config: _mqtt().mqttServer(),
        stop=lambda: _mqtt().stop()),
    "deconz": ManagedService(
        name="deconz",
        config_key="deconz",
        start=lambda config: _deconz().websocketClient(),
        stop=lambda: _deconz().stop()),
}


def component(name):
    """Registry key for a service.

    Namespaced because an integration also publishes its own, richer state
    under its plain name - homeAssistantWS owns "homeassistant".
    """
    return f"service.{name}"


def is_running(name):
    service = SERVICES[name]
    if service.running is not None:
        return bool(service.running())
    thread = _threads.get(name)
    return thread is not None and thread.is_alive()


def _publish(name):
    return statusRegistry.update(component(name), running=is_running(name),
                                 error=_errors.get(name))


def status(name=None):
    """Reportable state for one service, or all of them.

    Liveness is recomputed rather than read back from the registry, so a
    service that has just started - or has just died on its own thread - is
    reported accurately.
    """
    if name is not None:
        return _publish(name)
    return {service: _publish(service) for service in SERVICES}


def reset_for_tests():
    """Forget all supervised threads. Only used by the tests."""
    _threads.clear()
    _errors.clear()


def _run(name, service, service_config):
    """Thread body: record why a service died instead of losing the traceback."""
    try:
        service.start(service_config)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        _errors[name] = message
        logging.exception("The %s integration stopped with an error", name)
        statusRegistry.update(component(name), running=False, error=message)
        statusRegistry.event("service_state", service=name, enabled=True,
                             running=False, error=message)


def _start(name, service_config):
    service = SERVICES[name]
    logging.info("Starting %s integration", name)
    _errors[name] = None
    if not service.threaded:
        try:
            service.start(service_config)
        except Exception as e:
            _errors[name] = f"{type(e).__name__}: {e}"
            logging.exception("Could not start %s integration", name)
        return
    thread = threading.Thread(target=_run, args=[name, service, service_config],
                              name=f"{name}-service", daemon=True)
    _threads[name] = thread
    thread.start()


def _stop(name):
    service = SERVICES[name]
    logging.info("Stopping %s integration", name)
    try:
        service.stop()
    except Exception:
        logging.exception("Error stopping %s integration", name)
    thread = _threads.pop(name, None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=STOP_TIMEOUT_SECONDS)
    _errors[name] = None


def apply(bridgeConfig, changed_keys=None):
    """Reconcile the running services with the configuration.

    Args:
        bridgeConfig: the whole bridge configuration.
        changed_keys: when given, only these config keys are reconsidered. A
            service whose settings changed while it was up is restarted, so a
            new address or token is actually used.
    """
    results = {}
    with _lock:
        for name, service in SERVICES.items():
            if changed_keys is not None and service.config_key not in changed_keys:
                continue
            service_config = bridgeConfig["config"].get(service.config_key) or {}
            enabled = bool(service_config.get("enabled", False))
            running = is_running(name)
            reconfigured = changed_keys is not None and enabled and running

            if running and (not enabled or reconfigured):
                _stop(name)
                running = False
            if enabled and not running:
                _start(name, service_config)

            statusRegistry.update(component(name), enabled=enabled)
            results[name] = _publish(name)
            statusRegistry.event("service_state", service=name, enabled=enabled,
                                 running=results[name]["running"],
                                 error=results[name].get("error"))
    return results


def set_enabled(bridgeConfig, name, enabled):
    """Flip one integration on or off and make it take effect immediately."""
    if name not in SERVICES:
        raise KeyError(name)
    service_config = bridgeConfig["config"].setdefault(SERVICES[name].config_key, {})
    service_config["enabled"] = bool(enabled)
    return apply(bridgeConfig, changed_keys={SERVICES[name].config_key})[name]
