{
  pkgs,
  lib,
  diyhueModule,
  diyhuePackage,
  ...
}:
let
  # Test framework assigns 192.168.1.<N> based on alphabetical node order.
  # bridge → .1, hass → .2
  bridgeIp = "192.168.1.1";
  hassIp = "192.168.1.2";

  certPath = "/var/lib/diyhue/cert.pem";

  pythonWithYaml = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);

  # Drives the status page's websocket the way a browser would: through nginx,
  # over TLS, authenticated with the session cookie curl already obtained.
  statusWatcher = pkgs.writers.writePython3Bin "status-watcher"
    { libraries = [ pkgs.python3Packages.websocket-client ]; flakeIgnore = [ "E501" ]; }
    ''
      import json
      import ssl
      import sys
      import threading
      import urllib.request

      import websocket

      COOKIE_JAR = sys.argv[1]
      HOST = sys.argv[2]


      def cookie_header(path):
          parts = []
          with open(path) as handle:
              for line in handle:
                  line = line.rstrip("\n")
                  if line.startswith("#HttpOnly_"):
                      line = line[len("#HttpOnly_"):]
                  if not line.strip() or line.startswith("#"):
                      continue
                  fields = line.split("\t")
                  if len(fields) >= 7:
                      parts.append(f"{fields[5]}={fields[6]}")
          return "; ".join(parts)


      cookie = cookie_header(COOKIE_JAR)
      context = ssl._create_unverified_context()

      connection = websocket.create_connection(
          f"wss://{HOST}/status/ws",
          header=[f"Cookie: {cookie}"],
          sslopt={"cert_reqs": ssl.CERT_NONE},
          timeout=120,
      )

      first = json.loads(connection.recv())
      assert first["kind"] == "snapshot", first
      print("SNAPSHOT services:", ",".join(sorted(first["state"]["services"])))


      def trigger():
          request = urllib.request.Request(
              f"https://{HOST}/status/api/scan", data=b"{}",
              headers={"Cookie": cookie, "Content-Type": "application/json"})
          urllib.request.urlopen(request, context=context).read()


      threading.Timer(1.0, trigger).start()

      kinds = set()
      while True:
          event = json.loads(connection.recv())
          kinds.add(event["kind"])
          if event["kind"] == "light_found":
              print("LIGHT_FOUND:", event["name"])
          if event["kind"] == "scan_finished":
              break

      connection.close()
      print("KINDS:", ",".join(sorted(kinds)))
    '';
in
pkgs.testers.nixosTest {
  name = "diyhue-nginx";

  nodes = {
    bridge =
      { ... }:
      {
        imports = [ diyhueModule ];

        nixpkgs.overlays = [ (final: prev: { diyhue = diyhuePackage; }) ];

        # diyhue listens only on loopback; nginx terminates TLS using the same
        # MAC-bound cert and reverse-proxies to it. HTTPS in diyhue is disabled
        # because only one process serves :443 on this host.
        services.diyhue = {
          enable = true;
          mac = "00:11:22:33:44:55";
          bindAddress = "127.0.0.1";
          httpPort = 8080;
          noServeHttps = true;
          advertisedIp = bridgeIp;
          openFirewall = false;
          inherit certPath;
          # nginx must be able to read the cert.
          certGroup = "nginx";
        };

        # Don't auto-start: testScript primes config.yaml with the HA token first.
        systemd.services.diyhue.wantedBy = lib.mkForce [ ];

        services.nginx = {
          enable = true;
          recommendedProxySettings = true;

          virtualHosts."_" = {
            default = true;
            addSSL = true;
            sslCertificate = certPath;
            sslCertificateKey = certPath;

            locations."/" = {
              proxyPass = "http://127.0.0.1:8080";
              extraConfig = ''
                proxy_buffering off;
              '';
            };

            # The status page's live update channel. proxyWebsockets is what
            # forwards the Upgrade/Connection headers - without them Werkzeug
            # does not recognise the request as a websocket and Flask answers
            # 400. The timeouts stop nginx treating a socket that is idle
            # between events as a dead upstream.
            locations."/status/ws" = {
              proxyPass = "http://127.0.0.1:8080";
              proxyWebsockets = true;
              extraConfig = ''
                proxy_buffering off;
                proxy_read_timeout 3600s;
                proxy_send_timeout 3600s;
              '';
            };
          };
        };

        systemd.services.nginx = {
          after = [ "diyhue-cert.service" ];
          requires = [ "diyhue-cert.service" ];
        };

        environment.systemPackages = [ pythonWithYaml ];

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

            input_boolean.test_light.name = "Test Light";

            # Template light backed by input_boolean.test_light so HA can drive
            # on/off and diyHue's homeassistant_ws integration imports it.
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
          statusWatcher
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

    bridge.wait_for_unit("diyhue-cert.service")
    bridge.wait_for_unit("nginx.service")
    bridge.wait_for_open_port(80)
    bridge.wait_for_open_port(443)

    hass.wait_for_unit("home-assistant.service")
    hass.wait_for_open_port(8123)
    hass.wait_until_succeeds(
        "journalctl -u home-assistant.service --no-pager "
        "| grep -q 'Home Assistant initialized in'",
        timeout=120,
    )

    # === Bootstrap HA: onboard owner + obtain an access token. ===
    onboard = hass.succeed(
        "curl -fsS -X POST http://localhost:8123/api/onboarding/users "
        "-H 'Content-Type: application/json' "
        "-d '{\"client_id\":\"http://hass:8123/\",\"name\":\"test\","
        "\"username\":\"test\",\"password\":\"test\",\"language\":\"en\"}'"
    )
    auth_code = _json.loads(onboard)["auth_code"]

    token_res = hass.succeed(
        "curl -fsS -X POST http://localhost:8123/auth/token "
        f"-d 'grant_type=authorization_code&code={auth_code}"
        "&client_id=http://hass:8123/'"
    )
    ha_token = _json.loads(token_res)["access_token"]

    for step in ("core_config", "analytics"):
        hass.succeed(
            f"curl -fsS -X POST http://localhost:8123/api/onboarding/{step} "
            f"-H 'Authorization: Bearer {ha_token}'"
        )

    hass.wait_until_succeeds(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test | grep -q '\"state\":'"
    )
    initial_data = _json.loads(hass.succeed(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test"
    ))
    assert initial_data["state"] == "off", f"Expected off, got: {initial_data}"

    # === Bring diyhue up so config.yaml gets materialised, then patch it
    #     with the HA WS credentials and restart. ===
    bridge.systemctl("start diyhue")
    bridge.wait_for_open_port(8080)
    # Trigger save_config() via the loopback link-button PUT.
    bridge.succeed(
        "curl -fsS -X PUT -H 'Content-Type: application/json' "
        "-d '{\"linkbutton\":true}' http://localhost:8080/api/anybody/config"
    )
    bridge.wait_until_succeeds("test -f /var/lib/diyhue/config.yaml", timeout=10)
    bridge.systemctl("stop diyhue")

    bridge.succeed(
        "python3 -c \""
        "import yaml; "
        "f=open('/var/lib/diyhue/config.yaml'); c=yaml.safe_load(f); f.close(); "
        "c['homeassistant']={'enabled':True,'homeAssistantIp':'${hassIp}',"
        f"'homeAssistantPort':8123,'homeAssistantToken':'{ha_token}',"
        "'homeAssistantIncludeByDefault':True}; "
        "f=open('/var/lib/diyhue/config.yaml','w'); yaml.safe_dump(c,f); f.close()\""
    )

    bridge.systemctl("start diyhue")
    bridge.wait_for_open_port(8080)
    bridge.wait_until_succeeds(
        "journalctl -u diyhue --no-pager "
        "| grep -q 'Home Assistant Web Socket Authorisation complete'",
        timeout=60,
    )

    # === Verify nginx serves diyHue's MAC-bound cert (not nginx's snake-oil). ===
    cert_subject = hass.succeed(
        "openssl s_client -connect bridge:443 -servername bridge </dev/null 2>/dev/null "
        "| openssl x509 -noout -subject"
    )
    assert "001122fffe334455" in cert_subject, (
        f"nginx is not serving the diyHue cert. Got: {cert_subject}"
    )

    # === Web UI: static bundles reachable + full login round-trip. ===
    # Regression for the packaging bug where the Vite build's hashed bundles
    # were copied to flaskUI/assets/assets/ instead of being merged into
    # flaskUI/assets/. Flask's /assets static route then 404'd every
    # index-*.js, so the browser reported "Loading failed for the module".
    import re as _re

    def _csrf(page):
        m = _re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        assert m, f"no csrf_token in login page: {page[:200]}"
        return m.group(1)

    asset_ct = hass.succeed(
        "curl -fsSk -o /dev/null -w '%{content_type}' "
        "https://bridge/assets/index-BGcdHbbG.js"
    )
    assert "javascript" in asset_ct, f"JS bundle not served as JS: {asset_ct!r}"
    # CSS bundle (also from the Vite build) and an image that ships in the
    # repo's own assets dir — proves the two trees were merged, not nested.
    hass.succeed("curl -fsSk -o /dev/null https://bridge/assets/index-CyzsjfeZ.css")
    hass.succeed("curl -fsSk -o /dev/null https://bridge/assets/images/favicon.ico")

    # The SPA shell at / is login-gated: an unauthenticated GET redirects.
    root_code = hass.succeed(
        "curl -sk -o /dev/null -w '%{http_code}' https://bridge/"
    ).strip()
    assert root_code == "302", f"unauthenticated / should redirect, got {root_code}"

    # Wrong password is rejected (fresh session/CSRF).
    bad_page = hass.succeed("curl -fsSk -c /tmp/cj_bad https://bridge/login")
    bad_csrf = _csrf(bad_page)
    bad_body = hass.succeed(
        "curl -fsSk -b /tmp/cj_bad -c /tmp/cj_bad --referer https://bridge/login "
        f"--data-urlencode 'csrf_token={bad_csrf}' "
        "--data-urlencode 'email=admin@diyhue.org' "
        "--data-urlencode 'password=wrong' https://bridge/login"
    )
    assert "Bad login" in bad_body, f"bad password not rejected: {bad_body!r}"

    # Correct default credentials log in and land on the SPA shell.
    login_page = hass.succeed("curl -fsSk -c /tmp/cj https://bridge/login")
    csrf = _csrf(login_page)
    login_code = hass.succeed(
        "curl -sk -b /tmp/cj -c /tmp/cj -o /dev/null -w '%{http_code}' "
        "--referer https://bridge/login "
        f"--data-urlencode 'csrf_token={csrf}' "
        "--data-urlencode 'email=admin@diyhue.org' "
        "--data-urlencode 'password=changeme' https://bridge/login"
    ).strip()
    assert login_code == "302", f"valid login did not redirect (got {login_code})"

    index_html = hass.succeed("curl -fsSk -b /tmp/cj https://bridge/")
    assert "/assets/index-BGcdHbbG.js" in index_html, (
        f"authenticated / did not render the SPA shell: {index_html[:200]}"
    )
    # The SPA is a prebuilt release archive, so the only way to reach the status
    # page from it is the link diyHue injects into the served markup.
    assert 'href="/status/"' in index_html, (
        f"the SPA shell must link to the status page: {index_html[-400:]}"
    )

    # The status page is served by diyHue itself, not by the release bundle.
    status_html = hass.succeed("curl -fsSk -b /tmp/cj https://bridge/status/")
    assert "/assets/status.js" in status_html, (
        f"status page did not render: {status_html[:200]}"
    )
    hass.succeed("curl -fsSk -o /dev/null https://bridge/assets/status.js")
    hass.succeed("curl -fsSk -o /dev/null https://bridge/assets/status.css")

    # === Pair hueadm through nginx + drive everything else through it. ===
    bridge.succeed(
        "curl -fsS -X PUT -H 'Content-Type: application/json' "
        "-d '{\"linkbutton\":true}' http://localhost:8080/api/anybody/config"
    )
    pair = hass.succeed("hueadm --host bridge create-user nixos-test -j")
    username = _extract_json(pair, "[")[0]["success"]["username"]
    print("paired username:", username)

    # Sanity-check the HTTPS reverse-proxy path serves the API too.
    assert '"bridgeid"' in hass.succeed(
        f"curl -fsSk https://bridge/api/{username}/config"
    )

    # === Trigger discovery so diyHue imports the HA template light. ===
    hass.succeed(
        f"curl -fsS -X POST -H 'Content-Type: application/json' "
        f"-d '{{}}' http://bridge/api/{username}/lights"
    )
    bridge.wait_until_succeeds(
        "journalctl -u diyhue --no-pager | grep -q 'Add new light Diyhue Test'",
        timeout=180,
    )

    # === BEFORE: hueadm reports the light off. ===
    lights_data = _extract_json(
        hass.succeed(f"hueadm --host bridge --user {username} lights -j"), "{"
    )
    assert lights_data, "No HA lights discovered"
    light_id = next(iter(lights_data))
    print(f"light id {light_id} state BEFORE:", lights_data[light_id]["state"])
    assert lights_data[light_id]["state"]["on"] is False, (
        f"Expected light to be OFF before toggle, got: {lights_data[light_id]['state']}"
    )

    # === Flip via HA REST. ===
    hass.succeed(
        f"curl -fsS -X POST -H 'Authorization: Bearer {ha_token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"entity_id\":\"light.diyhue_test\"}}' "
        f"http://localhost:8123/api/services/light/turn_on"
    )

    # diyHue's stateFetch sync waits for the apiUser last_use_date to update,
    # which only happens because ProxyFix translates X-Forwarded-For so
    # request.remote_addr is the hass IP, not the nginx loopback.
    saw_on = False
    last_state = None
    for _ in range(60):
        lights_data = _extract_json(
            hass.succeed(f"hueadm --host bridge --user {username} lights -j"), "{"
        )
        last_state = lights_data[light_id]["state"]
        if last_state.get("on") is True:
            saw_on = True
            break
        time.sleep(0.5)

    print(f"light id {light_id} state AFTER:", last_state)
    assert saw_on, f"hueadm never observed ON state; last: {last_state}"

    final_data = _json.loads(hass.succeed(
        f"curl -fsS -H 'Authorization: Bearer {ha_token}' "
        "http://localhost:8123/api/states/light.diyhue_test"
    ))
    assert final_data["state"] == "on", f"HA reports light is not on: {final_data}"

    # === Status websocket, end to end through nginx. ===
    # Proves the /status/ws proxy location upgrades the connection and that
    # discovery events reach a browser live.
    watcher = hass.succeed("status-watcher /tmp/cj bridge")
    print("watcher output:", watcher)
    assert "SNAPSHOT services:" in watcher, watcher
    kinds = next(line for line in watcher.splitlines() if line.startswith("KINDS:"))
    for expected in ("scan_started", "protocol_ok", "scan_finished"):
        assert expected in kinds, f"missing {expected} in {kinds}"

    # === The include filter is reported, not silently applied. ===
    # A stock Home Assistant tags nothing, so with includeByDefault off the
    # scan legitimately finds nothing - the page has to say so.
    def _status():
        return _json.loads(hass.succeed("curl -fsSk -b /tmp/cj https://bridge/status/api/state"))

    hass.succeed(
        "curl -fsSk -b /tmp/cj -X POST -H 'Content-Type: application/json' "
        "-d '{\"homeAssistantIncludeByDefault\":false}' "
        "https://bridge/status/api/homeassistant"
    )
    hass.succeed(
        "curl -fsSk -b /tmp/cj -X POST -H 'Content-Type: application/json' "
        "-d '{}' https://bridge/status/api/homeassistant/test"
    )
    discovery = _status()["homeassistant"]["discovery"]
    assert discovery["entities_seen"] > 0, discovery
    assert discovery["entities_included"] == 0, discovery
    assert discovery["excluded_sample"], (
        f"the page must name entities the filter dropped: {discovery}")

    # Turning it back on includes them again, without a restart.
    hass.succeed(
        "curl -fsSk -b /tmp/cj -X POST -H 'Content-Type: application/json' "
        "-d '{\"homeAssistantIncludeByDefault\":true}' "
        "https://bridge/status/api/homeassistant"
    )
    hass.succeed(
        "curl -fsSk -b /tmp/cj -X POST -H 'Content-Type: application/json' "
        "-d '{}' https://bridge/status/api/homeassistant/test"
    )
    state = _status()
    discovery = state["homeassistant"]["discovery"]
    assert discovery["entities_included"] == discovery["entities_seen"], discovery

    # Included entities and registered lights are separate numbers: this bridge
    # has already scanned, so the light it imported has to be attributed to the
    # Home Assistant integration rather than left implicit in the entity count.
    assert state["protocols"]["homeassistant"]["lights"] > 0, (
        f"lights imported from Home Assistant must be counted: {state['protocols']}")
    assert state["protocols"]["wled"]["lights"] == 0, state["protocols"]["wled"]
  '';
}
