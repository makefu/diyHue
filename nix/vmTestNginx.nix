{
  pkgs,
  lib,
  diyhueModule,
  diyhuePackage,
  ...
}:
let
  # Test framework assigns 192.168.1.<N> based on alphabetical node order.
  # bridge → .1, hass → .2, proxy → .3
  proxyIp = "192.168.1.3";
  hassIp = "192.168.1.2";

  pythonWithYaml = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
in
pkgs.testers.nixosTest {
  name = "diyhue-nginx";

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
          # advertise the proxy so description.xml URLBase tells clients to use nginx
          advertisedIp = proxyIp;
        };

        # Don't auto-start: testScript patches config.yaml with the HA token first,
        # then starts the service manually.
        systemd.services.diyhue.wantedBy = lib.mkForce [ ];

        environment.systemPackages = [ pythonWithYaml ];

        networking.firewall.enable = false;
      };

    proxy =
      { ... }:
      {
        services.nginx = {
          enable = true;
          recommendedProxySettings = true;

          virtualHosts."_" = {
            default = true;
            locations."/" = {
              proxyPass = "http://bridge";
              extraConfig = ''
                proxy_buffering off;
              '';
            };
          };

          streamConfig = ''
            upstream diyhue_https {
              server bridge:443;
            }
            server {
              listen 443;
              proxy_pass diyhue_https;
            }
          '';
        };

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
            "input_boolean"
            "template"
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

            input_boolean = {
              test_light = {
                name = "Test Light";
              };
            };

            # Template light backed by input_boolean.test_light so HA can drive on/off
            # and diyHue's homeassistant_ws integration picks it up as a Hue light.
            template = [
              {
                light = [
                  {
                    name = "Diyhue Test";
                    unique_id = "diyhue_test";
                    state = "{{ states('input_boolean.test_light') }}";
                    turn_on = {
                      service = "input_boolean.turn_on";
                      target.entity_id = "input_boolean.test_light";
                    };
                    turn_off = {
                      service = "input_boolean.turn_off";
                      target.entity_id = "input_boolean.test_light";
                    };
                  }
                ];
              }
            ];
          };
        };

        environment.systemPackages = with pkgs; [
          curl
          openssl
          hueadm
        ];

        networking.firewall.enable = false;
      };
  };

  testScript = ''
    import json as _json
    import time

    def _extract_json(text, opener):
        # hueadm sometimes prefixes JSON with status lines.
        return _json.loads(text[text.index(opener):])

    start_all()

    # === Wait for nginx + HA. diyhue is intentionally NOT auto-started. ===
    proxy.wait_for_unit("nginx.service")
    proxy.wait_for_open_port(80)
    proxy.wait_for_open_port(443)

    hass.wait_for_unit("home-assistant.service")
    hass.wait_for_open_port(8123)
    # HA finishes loading components asynchronously; wait for full init.
    hass.wait_until_succeeds(
        "journalctl -u home-assistant.service --no-pager | grep -q 'Home Assistant initialized in'",
        timeout=120,
    )

    # === Bootstrap HA: onboard owner user + obtain an access token. ===
    onboard = hass.succeed(
        "curl -fsS -X POST http://localhost:8123/api/onboarding/users "
        "-H 'Content-Type: application/json' "
        "-d '{\"client_id\":\"http://hass:8123/\",\"name\":\"test\","
        "\"username\":\"test\",\"password\":\"test\",\"language\":\"en\"}'"
    )
    auth_code = _json.loads(onboard)["auth_code"]
    print("HA auth_code obtained")

    token_res = hass.succeed(
        "curl -fsS -X POST http://localhost:8123/auth/token "
        f"-d 'grant_type=authorization_code&code={auth_code}"
        "&client_id=http://hass:8123/'"
    )
    ha_token = _json.loads(token_res)["access_token"]
    print("HA access_token obtained")

    # Finish the remaining onboarding steps so integrations + frontend settle.
    for step in ("core_config", "analytics"):
        hass.succeed(
            f"curl -fsS -X POST http://localhost:8123/api/onboarding/{step} "
            f"-H 'Authorization: Bearer {ha_token}'"
        )

    # Confirm the template light exists.
    hass.wait_until_succeeds(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test | grep -q '\"state\":'"
    )
    initial_state = hass.succeed(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test"
    )
    print("HA initial light state:", initial_state)
    initial_data = _json.loads(initial_state)
    assert initial_data["state"] == "off", f"Expected off, got: {initial_state}"

    # === Start diyhue with HA WS integration pre-configured. ===
    # First run to materialise the default config.yaml. diyHue only persists when
    # save_config() is invoked, so we trigger that with a no-op PUT that flips
    # linkbutton (loopback PUTs bypass the auth check via restful.py:39).
    bridge.systemctl("start diyhue")
    bridge.wait_for_open_port(80)
    bridge.succeed(
        "curl -fsS -X PUT -H 'Content-Type: application/json' "
        "-d '{\"linkbutton\":true}' http://localhost/api/anybody/config"
    )
    bridge.wait_until_succeeds("test -f /var/lib/diyhue/config.yaml", timeout=10)
    bridge.systemctl("stop diyhue")

    # Patch config.yaml: enable homeassistant + plug in IP/port/token.
    bridge.succeed(
        "python3 -c \""
        "import yaml; "
        "f=open('/var/lib/diyhue/config.yaml'); c=yaml.safe_load(f); f.close(); "
        "c['homeassistant']={'enabled':True,'homeAssistantIp':'${hassIp}',"
        f"'homeAssistantPort':8123,'homeAssistantToken':'{ha_token}',"
        "'homeAssistantIncludeByDefault':True,'homeAssistantUseHttps':False}; "
        "f=open('/var/lib/diyhue/config.yaml','w'); yaml.safe_dump(c,f); f.close()\""
    )

    bridge.systemctl("start diyhue")
    bridge.wait_for_open_port(80)

    # Wait for diyHue to authenticate against HA's WS API.
    bridge.wait_until_succeeds(
        "journalctl -u diyhue --no-pager "
        "| grep -q 'Home Assistant Web Socket Authorisation complete'",
        timeout=60,
    )

    # === Pair hueadm through nginx. ===
    bridge.succeed(
        "curl -fsS -X PUT -H 'Content-Type: application/json' "
        "-d '{\"linkbutton\":true}' http://localhost/api/anybody/config"
    )
    pair = hass.succeed("hueadm --host proxy create-user nixos-test -j")
    pair_data = _extract_json(pair, "[")
    username = pair_data[0]["success"]["username"]
    print("paired username:", username)

    # === Trigger light discovery so diyHue imports the HA template light. ===
    hass.succeed(
        f"curl -fsS -X POST -H 'Content-Type: application/json' "
        f"-d '{{}}' http://proxy/api/{username}/lights"
    )
    bridge.wait_until_succeeds(
        "journalctl -u diyhue --no-pager "
        "| grep -q 'HomeAssistant_ws: found light'",
        timeout=120,
    )
    # scanForLights runs other protocols (WLED, tasmota...) sequentially after the WS
    # discovery; new lights are only persisted once the *whole* scan finishes and the
    # `Add new light` branch fires. Wait for that log line specifically.
    bridge.wait_until_succeeds(
        "journalctl -u diyhue --no-pager | grep -q 'Add new light Diyhue Test'",
        timeout=180,
    )

    # === Verify hueadm sees the imported light + initial OFF state. ===
    lights = hass.succeed(f"hueadm --host proxy --user {username} lights -j")
    print("discovered lights via hueadm:", lights)
    lights_data = _extract_json(lights, "{")
    assert len(lights_data) >= 1, f"No HA lights discovered: {lights}"
    light_id = next(iter(lights_data))
    print(f"light id {light_id} state BEFORE:", lights_data[light_id]["state"])
    assert lights_data[light_id]["state"]["on"] is False, (
        f"Expected light to be OFF before toggle, got: {lights_data[light_id]['state']}"
    )

    # === Flip the HA template light ON via HA REST. ===
    hass.succeed(
        f"curl -fsS -X POST -H 'Authorization: Bearer {ha_token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"entity_id\":\"light.diyhue_test\"}}' "
        f"http://localhost:8123/api/services/light/turn_on"
    )

    # Wait for the WS state update to propagate into diyHue.
    saw_on = False
    last_state = None
    for _ in range(40):
        lights = hass.succeed(f"hueadm --host proxy --user {username} lights -j")
        lights_data = _extract_json(lights, "{")
        last_state = lights_data[light_id]["state"]
        if last_state.get("on") is True:
            saw_on = True
            break
        time.sleep(0.5)

    print(f"light id {light_id} state AFTER:", last_state)
    assert saw_on, f"hueadm never observed ON state; last: {last_state}"

    # === Sanity: HA still exposes the light as on. ===
    final_state = hass.succeed(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test"
    )
    final_data = _json.loads(final_state)
    assert final_data["state"] == "on", f"HA reports light is not on: {final_state}"
  '';
}
