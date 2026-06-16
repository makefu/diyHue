{
  pkgs,
  diyhueModule,
  diyhuePackage,
  ...
}:
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
    import json as _json
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
  '';
}
