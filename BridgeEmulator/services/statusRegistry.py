"""In-memory status of everything the user can see on the /status page.

Two things live here:

* a snapshot dict, keyed by component ("scan", "homeassistant", "mqtt", ...),
  holding the current state of that component. New subscribers get this first
  so a page load never has to wait for the next event.
* a fan-out of discrete events (a light was found, a scan finished, a service
  was toggled) delivered to every subscriber's queue.

Publishing must never block a worker thread, so a subscriber whose queue is
full simply loses events - it will resynchronise from the snapshot.
"""

import queue
import threading
from collections import deque
from datetime import datetime, timezone

# A slow browser must not be able to stall discovery; once this many events are
# outstanding the subscriber starts dropping them.
SUBSCRIBER_QUEUE_SIZE = 256
MAX_SUBSCRIBERS = 16
EVENT_HISTORY = 100

_lock = threading.Lock()
_components = {}
_history = deque(maxlen=EVENT_HISTORY)
_subscribers = []
_sequence = 0


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset():
    """Drop all state. Only used by the tests."""
    global _sequence
    with _lock:
        _components.clear()
        _history.clear()
        _subscribers.clear()
        _sequence = 0


def update(component, **fields):
    """Merge ``fields`` into ``component``'s state and return the new state."""
    with _lock:
        state = _components.setdefault(component, {})
        state.update(fields)
        state["updated"] = _timestamp()
        return dict(state)


def replace(component, state):
    """Replace ``component``'s state wholesale, for callers that own it fully."""
    with _lock:
        _components[component] = {**state, "updated": _timestamp()}
        return dict(_components[component])


def get(component):
    with _lock:
        return dict(_components.get(component, {}))


def event(kind, **fields):
    """Publish an event to every subscriber and to the replay history."""
    global _sequence
    with _lock:
        _sequence += 1
        message = {"kind": kind, "seq": _sequence, "time": _timestamp(), **fields}
        _history.append(message)
        targets = list(_subscribers)
    for target in targets:
        try:
            target.put_nowait(message)
        except queue.Full:
            pass
    return message


def snapshot():
    """Everything a freshly connected client needs to render the page."""
    with _lock:
        return {
            "components": {name: dict(state) for name, state in _components.items()},
            "events": list(_history),
            "seq": _sequence,
        }


def subscribe():
    """Register a new event sink. Returns None when the cap is reached."""
    with _lock:
        if len(_subscribers) >= MAX_SUBSCRIBERS:
            return None
        sink = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        _subscribers.append(sink)
        return sink


def unsubscribe(sink):
    with _lock:
        if sink in _subscribers:
            _subscribers.remove(sink)


def drain(sink):
    """Return every event queued for ``sink`` without blocking."""
    events = []
    while True:
        try:
            events.append(sink.get_nowait())
        except queue.Empty:
            return events
