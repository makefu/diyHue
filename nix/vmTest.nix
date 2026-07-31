{
  pkgs,
  diyhueModule,
  diyhuePackage,
  ...
}:
let
  # A Home Assistant that misbehaves on purpose. Real HA cannot be made to
  # reject a token or return an empty entity list on demand, but those are
  # exactly the cases that used to leave the web interface showing a scan that
  # never finished.
  stubHomeAssistant = pkgs.writers.writePython3Bin "stub-home-assistant"
    { libraries = [ pkgs.python3Packages.websockets ]; flakeIgnore = [ "E501" ]; }
    ''
      import asyncio
      import json
      import sys

      import websockets

      MODE = sys.argv[1]
      PORT = int(sys.argv[2])

      # Two entities that exercise both model-id branches: a colour light and a
      # switch, which carries no supported_color_modes at all.
      ENTITIES = [
          {
              "entity_id": "light.stub_strip",
              "state": "on",
              "attributes": {
                  "friendly_name": "Stub strip",
                  "supported_color_modes": ["hs"],
                  "brightness": 128,
                  "hs_color": [180.0, 50.0],
              },
          },
          {
              "entity_id": "switch.stub_plug",
              "state": "off",
              "attributes": {"friendly_name": "Stub plug"},
          },
      ]


      async def handler(websocket):
          await websocket.send(json.dumps({"type": "auth_required", "ha_version": "2026.7.0"}))
          await websocket.recv()
          if MODE == "invalid_auth":
              await websocket.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
              return
          await websocket.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7.0"}))
          while True:
              message = json.loads(await websocket.recv())
              reply = {"id": message.get("id"), "type": "result", "success": True, "result": None}
              if message.get("type") == "get_states":
                  # An empty list used to leave discovery blocked for its full
                  # 60 second timeout.
                  reply["result"] = ENTITIES if MODE == "entities" else []
              await websocket.send(json.dumps(reply))


      async def main():
          async with websockets.serve(handler, "127.0.0.1", PORT):
              await asyncio.Future()


      asyncio.run(main())
    '';
in
pkgs.testers.nixosTest {
  name = "diyhue";

  nodes = {
    bridge =
      { ... }:
      {
        imports = [ diyhueModule ];

        nixpkgs.overlays = [ (final: prev: { diyhue = diyhuePackage; }) ];

        services.diyhue = {
          enable = true;
          mac = "00:11:22:33:44:55";
          httpPort = 80;
          httpsPort = 443;
          bindAddress = "0.0.0.0";
          advertisedIp = "192.168.1.2";
        };

        environment.systemPackages = [ stubHomeAssistant ];

        networking.firewall.enable = false;
      };

    hass =
      { pkgs, ... }:
      {
        services.home-assistant = {
          enable = true;
          configDir = "/var/lib/hass";
          extraComponents = [
            "default_config"
            "hue"
            "ssdp"
            "zeroconf"
          ];
          config = {
            homeassistant = {
              name = "test";
              latitude = "0.0";
              longitude = "0.0";
              elevation = 0;
              unit_system = "metric";
              time_zone = "UTC";
            };
            http = {
              server_host = "0.0.0.0";
              server_port = 8123;
            };
            logger.default = "info";
          };
        };

        networking.firewall.enable = false;
      };
  };

  testScript = ''
    import json as _json
    import time

    start_all()

    bridge.wait_for_unit("diyhue.service")
    bridge.wait_for_open_port(80)

    # description.xml proves SSDP advertisement payload is served
    bridge.succeed(
        "curl -fsS http://localhost/description.xml | grep -qi 'hue bridge'"
    )

    # /api/config is the unauthenticated bridge-id endpoint HA hits during discovery
    bridge.succeed(
        "curl -fsS http://localhost/api/config | tee /tmp/config.json"
    )
    bridge.succeed("grep -q '\"bridgeid\"' /tmp/config.json")
    bridge.succeed("grep -q '\"mac\"' /tmp/config.json")

    # Press the link button via the auth-bypassed loopback PUT.
    # restful.py:300 sets lastlinkbuttonpushed=now when {\"linkbutton\":true} is PUT.
    bridge.succeed(
        "curl -fsS -X PUT -H 'Content-Type: application/json' "
        "-d '{\"linkbutton\":true}' http://localhost/api/anybody/config"
    )

    # Cross-node pairing: hass POSTs /api with a devicetype.
    # NewUser.post returns [{\"success\": {\"username\": \"...\"}}] when link button was just pressed.
    hass.wait_for_unit("multi-user.target")
    hass.wait_until_succeeds("curl -fsS http://bridge/api/config", timeout=60)

    pair = hass.succeed(
        "curl -fsS -X POST -H 'Content-Type: application/json' "
        "-d '{\"devicetype\":\"hass#nixostest\"}' http://bridge/api/"
    )
    print("pair response:", pair)
    pair_data = _json.loads(pair)
    assert "success" in pair_data[0], f"Pairing failed: {pair}"
    username = pair_data[0]["success"]["username"]
    assert username, f"No username returned: {pair}"
    print("paired username:", username)

    # With the new username, the lights endpoint must answer with a JSON object.
    lights = hass.succeed(f"curl -fsS http://bridge/api/{username}/lights")
    print("lights response:", lights)
    assert lights.startswith("{"), f"Lights endpoint malformed: {lights}"

    # And the full config endpoint must include the bridge id and mac.
    cfg = hass.succeed(f"curl -fsS http://bridge/api/{username}/config")
    assert '"bridgeid"' in cfg, cfg
    assert '"mac"' in cfg, cfg
    print("authenticated config OK")

    # Home Assistant came up alongside it (proves no port clash, package coexistence).
    hass.wait_for_unit("home-assistant.service")
    hass.wait_for_open_port(8123)
    hass.succeed("curl -fsS http://localhost:8123/")

    # === Status page ===
    import re as _re

    def _csrf(page):
        m = _re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        assert m, f"no csrf_token in login page: {page[:200]}"
        return m.group(1)

    def _login():
        page = bridge.succeed("curl -fsS -c /tmp/cj http://localhost/login")
        code = bridge.succeed(
            "curl -s -b /tmp/cj -c /tmp/cj -o /dev/null -w '%{http_code}' "
            "--referer http://localhost/login "
            f"--data-urlencode 'csrf_token={_csrf(page)}' "
            "--data-urlencode 'email=admin@diyhue.org' "
            "--data-urlencode 'password=changeme' http://localhost/login"
        ).strip()
        assert code == "302", f"login did not redirect: {code}"

    def _state():
        return _json.loads(bridge.succeed("curl -fsS -b /tmp/cj http://localhost/status/api/state"))

    def _post(path, body="{}"):
        return _json.loads(bridge.succeed(
            f"curl -fsS -b /tmp/cj -X POST -H 'Content-Type: application/json' "
            f"-d '{body}' http://localhost{path}"
        ))

    def _wait_for(predicate, what, timeout=60):
        # The state endpoint answers JSON with compact separators, so poll and
        # inspect it properly instead of grepping for punctuation.
        deadline = time.monotonic() + timeout
        while True:
            state = _state()
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"timed out waiting for {what}: {_json.dumps(state)[:800]}")
            time.sleep(1)

    # The page and its API are login-gated, like the SPA shell.
    unauth = bridge.succeed(
        "curl -s -o /dev/null -w '%{http_code}' http://localhost/status/"
    ).strip()
    assert unauth == "302", f"unauthenticated /status/ should redirect, got {unauth}"

    _login()

    # Its assets ship in this repository rather than the diyHueUI release zip.
    bridge.succeed("curl -fsS -o /dev/null http://localhost/assets/status.js")
    bridge.succeed("curl -fsS -o /dev/null http://localhost/assets/status.css")
    assert "/assets/status.js" in bridge.succeed("curl -fsS -b /tmp/cj http://localhost/status/")

    state = _state()
    assert set(state["services"]) == {"homeassistant", "mqtt", "deconz"}, state["services"]
    assert "wled" in state["protocols"], state["protocols"]
    assert state["log"], "the status API must expose a log tail"

    # === Integrations toggle without restarting the service ===
    started_at = bridge.succeed(
        "systemctl show diyhue -p ExecMainStartTimestamp --value"
    ).strip()

    # A discovery-only protocol: the flag is all that changes.
    _post("/status/api/service/wled", '{"enabled":false}')
    assert _state()["protocols"]["wled"]["enabled"] is False
    _post("/status/api/service/wled", '{"enabled":true}')
    assert _state()["protocols"]["wled"]["enabled"] is True

    # === A Home Assistant that refuses the connection ===
    # Port 9 (discard) is closed, so connecting fails immediately.
    _post("/status/api/homeassistant",
          '{"enabled":true,"homeAssistantIp":"127.0.0.1","homeAssistantPort":9}')
    ha = _wait_for(lambda s: s["homeassistant"]["last_error"],
                   "an unreachable Home Assistant to be reported")["homeassistant"]
    assert ha["enabled"] is True and ha["connected"] is False, ha

    # A broken integration must not stop the scan or leave it stuck "active".
    _post("/status/api/scan")
    scan = _wait_for(lambda s: s["scan"].get("state") == "idle",
                     "the scan to finish", timeout=240)["scan"]
    assert scan["protocols"]["homeassistant"]["state"] == "error", scan["protocols"]
    assert scan["protocols"]["wled"]["state"] == "ok", (
        f"a failing protocol must not skip the rest: {scan['protocols']}")
    lastscan = _json.loads(bridge.succeed(
        f"curl -fsS http://localhost/api/{username}/lights/new"))["lastscan"]
    assert lastscan != "active", f"scanResult stuck active: {lastscan}"

    # === A Home Assistant that rejects the token ===
    bridge.succeed("systemd-run --unit=stub-ha-invalid "
                   "${stubHomeAssistant}/bin/stub-home-assistant invalid_auth 8124")
    bridge.wait_for_open_port(8124)
    _post("/status/api/homeassistant",
          '{"enabled":true,"homeAssistantIp":"127.0.0.1","homeAssistantPort":8124,'
          '"homeAssistantToken":"wrong"}')
    ha = _wait_for(
        lambda s: "authentication failed" in (s["homeassistant"]["last_error"] or ""),
        "the rejected token to be reported")["homeassistant"]
    assert ha["authenticated"] is False, ha
    bridge.succeed("systemctl stop stub-ha-invalid")

    # === A Home Assistant with nothing to offer ===
    bridge.succeed("systemd-run --unit=stub-ha-empty "
                   "${stubHomeAssistant}/bin/stub-home-assistant empty 8125")
    bridge.wait_for_open_port(8125)
    _post("/status/api/homeassistant",
          '{"enabled":true,"homeAssistantIp":"127.0.0.1","homeAssistantPort":8125,'
          '"homeAssistantToken":"good","homeAssistantIncludeByDefault":true}')
    _wait_for(lambda s: s["homeassistant"]["authenticated"],
              "the stub Home Assistant to authenticate")

    # The test call must return promptly rather than block for the 60s
    # discovery timeout.
    before = time.monotonic()
    result = _post("/status/api/homeassistant/test")
    elapsed = time.monotonic() - before
    assert result["ok"] is True, result
    assert elapsed < 30, f"an empty entity list blocked for {elapsed:.0f}s"
    assert result["status"]["discovery"]["entities_seen"] == 0, result["status"]

    bridge.succeed("systemctl stop stub-ha-empty")

    # === Included entities are not lights until a scan registers them ===
    # The reported symptom: the status page said every entity was included while
    # the app's light list stayed empty, because no scan had ever run.
    bridge.succeed("systemd-run --unit=stub-ha-entities "
                   "${stubHomeAssistant}/bin/stub-home-assistant entities 8126")
    bridge.wait_for_open_port(8126)
    _post("/status/api/homeassistant",
          '{"enabled":true,"homeAssistantIp":"127.0.0.1","homeAssistantPort":8126,'
          '"homeAssistantToken":"good","homeAssistantIncludeByDefault":true}')
    state = _wait_for(
        lambda s: s["homeassistant"]["discovery"]["entities_included"] == 2,
        "the stub entities to pass the include filter")
    assert state["protocols"]["homeassistant"]["lights"] == 0, (
        "entities must not count as registered lights before a scan: "
        f"{state['protocols']['homeassistant']}")

    _post("/status/api/scan")
    state = _wait_for(lambda s: s["scan"].get("state") == "idle",
                      "the scan to register the stub entities", timeout=240)
    assert state["protocols"]["homeassistant"]["lights"] == 2, (
        f"the scan must register both entities: {state['protocols']['homeassistant']}")
    lights = _json.loads(bridge.succeed(
        f"curl -fsS http://localhost/api/{username}/lights"))
    names = {light["name"] for light in lights.values()}
    assert {"Stub plug", "Stub strip"} <= names, (
        f"the app's light list must show the registered entities: {sorted(names)}")

    bridge.succeed("systemctl stop stub-ha-entities")

    # === Disabling and re-enabling takes effect immediately ===
    bridge.succeed("systemd-run --unit=stub-ha-empty2 "
                   "${stubHomeAssistant}/bin/stub-home-assistant empty 8125")
    bridge.wait_for_open_port(8125)
    _post("/status/api/homeassistant",
          '{"enabled":true,"homeAssistantIp":"127.0.0.1","homeAssistantPort":8125,'
          '"homeAssistantToken":"good","homeAssistantIncludeByDefault":true}')
    _wait_for(lambda s: s["homeassistant"]["authenticated"],
              "the stub Home Assistant to authenticate")
    _post("/status/api/service/homeassistant", '{"enabled":false}')
    assert _state()["services"]["homeassistant"]["running"] is False
    _post("/status/api/service/homeassistant", '{"enabled":true}')
    _wait_for(lambda s: s["homeassistant"]["authenticated"],
              "Home Assistant to reconnect after being re-enabled")

    still_started_at = bridge.succeed(
        "systemctl show diyhue -p ExecMainStartTimestamp --value"
    ).strip()
    assert still_started_at == started_at, (
        f"diyhue restarted ({started_at!r} -> {still_started_at!r}); "
        "integrations must be switchable at runtime"
    )
    bridge.succeed("systemctl stop stub-ha-empty2")
  '';
}
