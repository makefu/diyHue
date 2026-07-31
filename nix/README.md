# diyHue Nix packaging

This directory packages diyHue as a Nix flake, exposes a NixOS service module,
and ships a pytest suite plus two VM integration tests. Everything lives under
`nix/`; the flake entrypoint is `../flake.nix`.

## Flake outputs

```
.#packages.<system>.diyhue       # buildPythonApplication-style derivation
.#packages.<system>.default      # alias for the above

.#nixosModules.diyhue            # NixOS service module
.#nixosModules.default           # alias for the above

.#checks.<system>.unitTests      # pytest suite, no VM
.#checks.<system>.vmTest         # baseline: diyHue + Home Assistant on one VLAN
.#checks.<system>.vmTestNginx    # diyHue behind nginx, hueadm-driven e2e

.#devShells.<system>.default     # python3 + pytest + openssl + libfaketime
```

The flake input pins `github:NixOS/nixpkgs/nixos-unstable`; lock file lives
at `../flake.lock`.

## Build the package

```bash
nix build .#diyhue
./result/bin/diyhue --help
./result/bin/diyhue-genCert 001122fffe334455 ./tmp/cfg   # standalone cert gen
```

`diyhue --help` lists every CLI flag, including the Nix-added
`--cert-path` and `--no-cert-gen` options.

## Use the NixOS module

Import the flake's module from your system flake and configure
`services.diyhue`:

```nix
{
  inputs.diyhue.url = "github:diyhue/diyHue";

  outputs = { self, nixpkgs, diyhue }: {
    nixosConfigurations.bridge = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        diyhue.nixosModules.default
        {
          nixpkgs.overlays = [
            (final: prev: { diyhue = diyhue.packages.${final.system}.default; })
          ];

          services.diyhue = {
            enable = true;
            mac = "00:11:22:33:44:55";    # required, must be stable
            advertisedIp = "192.168.1.10"; # SSDP / mDNS URLBase
          };
        }
      ];
    };
  };
}
```

### Module options

| option | default | what it does |
| --- | --- | --- |
| `enable` | `false` | Turn the service on. |
| `package` | `pkgs.diyhue` | Override the diyhue derivation. |
| `mac` | — (required) | MAC address used to derive the bridge serial and TLS cert. |
| `bindAddress` | `"0.0.0.0"` | Listener bind address. |
| `advertisedIp` | `null` | IP advertised via SSDP/mDNS; `null` lets diyHue auto-detect. |
| `httpPort` | `80` | HTTP port. Hue clients expect 80. |
| `httpsPort` | `443` | HTTPS port. Hue clients expect 443. |
| `noServeHttps` | `false` | Disable the HTTPS listener (e.g. when a reverse proxy terminates TLS). |
| `generateCert` | `true` | Run the `diyhue-cert` oneshot to (re)generate the MAC-bound cert before diyHue starts. |
| `certPath` | `"/var/lib/diyhue/cert.pem"` | Where the cert lives. Pass this to a reverse proxy as well. |
| `certGroup` | `"diyhue"` | Group that owns the cert. Set to `"nginx"` (and add nginx to that group) when a proxy needs to read it. |
| `openFirewall` | `true` | Open TCP `httpPort`, `httpsPort` and UDP 1900/1982/2100. |
| `extraArgs` | `[]` | Extra raw CLI arguments to append. |

### Hardening

The systemd unit ships with `ProtectSystem=strict`, `NoNewPrivileges`,
`PrivateTmp`, `MemoryDenyWriteExecute`, a tight `RestrictAddressFamilies`,
and `CAP_NET_BIND_SERVICE` so the unprivileged `diyhue` user can bind 80/443.
State (config.yaml, cert.pem, light/group/scene yaml) lives in
`/var/lib/diyhue` via `StateDirectory`.

## Reverse-proxy recipe (nginx terminating TLS with the diyHue cert)

The `vmTestNginx` check is the canonical example. Sketch:

```nix
{
  services.diyhue = {
    enable = true;
    mac = "00:11:22:33:44:55";
    bindAddress = "127.0.0.1";
    httpPort = 8080;
    noServeHttps = true;          # nginx owns 443
    advertisedIp = "192.168.1.10";
    certPath = "/var/lib/diyhue/cert.pem";
    certGroup = "nginx";          # let nginx read the cert
  };

  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    virtualHosts."_" = {
      default = true;
      addSSL = true;
      sslCertificate = "/var/lib/diyhue/cert.pem";
      sslCertificateKey = "/var/lib/diyhue/cert.pem";
      locations."/".proxyPass = "http://127.0.0.1:8080";

      # The status page at /status/ streams live updates over a websocket.
      locations."/status/ws" = {
        proxyPass = "http://127.0.0.1:8080";
        proxyWebsockets = true;       # forwards Upgrade / Connection
        extraConfig = ''
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
}
```

Why this works:
- `proxyWebsockets = true` is what emits `proxy_http_version 1.1` plus the
  `Upgrade` / `Connection` headers. `recommendedProxySettings` does **not**
  include them, and without them Werkzeug does not recognise the request as a
  websocket, so Flask answers `400 Bad Request` and the status page silently
  falls back to polling. The long read/send timeouts stop nginx from dropping
  a socket that is idle between events (the page also sends a keepalive every
  25 seconds).
- `diyhue-cert.service` is a oneshot the module emits when `generateCert =
  true`. It runs `${pkg}/bin/diyhue-genCert` with all the binaries it needs
  (python3, openssl, libfaketime, bash) already on its PATH, writes
  `cert.pem` (concatenated key + cert), and chowns it `diyhue:certGroup`
  mode 0640.
- nginx is ordered after that oneshot, so it always finds the cert at boot.
- The patched diyHue source includes a `werkzeug.middleware.proxy_fix.ProxyFix`
  wrap, so `request.remote_addr` reflects the real client IP coming through
  the proxy (not the nginx loopback). This keeps the `apiUser.last_use_date`
  bookkeeping in `flaskUI/restful.py` accurate, which is what triggers
  `services.stateFetch` to re-sync within a couple of seconds instead of
  the 300 second idle window.

## Bring your own cert

Set `generateCert = false` and point `certPath` at the file you provide.
diyHue is launched with `--no-cert-gen` and will fail loudly if the file is
missing rather than silently rolling a fresh one.

## Run the tests

```bash
nix flake check -L                                # everything
nix build .#checks.x86_64-linux.unitTests -L      # pytest, seconds
nix build .#checks.x86_64-linux.vmTest -L         # baseline
nix build .#checks.x86_64-linux.vmTestNginx -L    # nginx + hueadm + HA e2e
```

Upstream CI only smoke-runs the docker image for 15 seconds, so the flake
checks are the test gate. During development the same pytest suite runs
straight from the dev shell:

```bash
nix develop --command bash -c 'cd BridgeEmulator && pytest -q tests'
```

### What each test covers

`unitTests` (pytest, no VM):

- Home Assistant state translation against payloads captured from a real
  instance, the `diyhue:` include filter truth table, and the colour-mode →
  Hue model mapping.
- Discovery keeps going when one protocol raises, and never leaves the scan
  marked `active`.
- `serviceManager` starts, stops and restarts integrations from config.
- `/status/api/state` counts the lights each integration actually registered,
  which is a different number from the entities its include filter accepts.

`vmTest` (two nodes, ~50s):

- diyHue is reachable on its standard ports.
- SSDP `description.xml` advertises as a Hue bridge.
- `/api/config` exposes `bridgeid` + `mac`.
- A `linkbutton:true` PUT pairs a Hue API user.
- A second VM (Home Assistant) can pair and query `/api/<user>/lights`
  through the network.
- The status page is login-gated and serves its own assets.
- A stub Home Assistant websocket server (`stub-home-assistant`) reproduces
  the failure modes a real HA cannot be asked for: connection refused, a
  rejected token, and an empty entity list. Each is reported through
  `/status/api/state`, and a scan still completes with the other protocols
  intact.
- The stub's `entities` mode serves a colour light and a switch, proving that
  entities passing the include filter count as `0` registered lights until a
  scan runs, and appear in `/api/<user>/lights` afterwards.
- Integrations are enabled and disabled at runtime; the test asserts
  `ExecMainStartTimestamp` is unchanged, i.e. diyhue never restarted.

`vmTestNginx` (two nodes, ~70s):

- nginx terminates TLS using the cert produced by `diyhue-cert.service`;
  the served cert subject contains the MAC-derived CN (`001122fffe334455`).
- `hueadm` (a real Hue CLI from nixpkgs) pairs through nginx and reads
  `/api/.../config`, `/lights` and `/groups`.
- HA exposes a `template:` light backed by `input_boolean.test_light`.
- The test patches `/var/lib/diyhue/config.yaml` with the HA long-lived
  access token, restarts diyhue, and waits for the WebSocket handshake.
- A scan-for-lights call imports the HA light. `hueadm` reports it `off`.
- The test flips the light on via HA's REST service call. Within ~2s
  `hueadm` observes the new state, proving end-to-end propagation:
  HA template script → input_boolean → template recomputed → HA WS
  `state_changed` event → diyHue `latest_states` → `stateFetch` →
  `bridgeConfig["lights"]` → REST → nginx → hueadm.
- `status-watcher` opens `wss://bridge/status/ws` the way a browser would,
  triggers a scan and asserts the `scan_started` → `protocol_ok` →
  `scan_finished` events arrive live. This is the regression guard for the
  `proxyWebsockets` requirement above.
- With `homeAssistantIncludeByDefault` off, the status API reports
  `entities_seen > 0` and `entities_included == 0` with a sample of the
  entities the filter dropped - the reason a stock Home Assistant yields no
  lights.

## Source patches the package applies

Documented for anyone diffing against upstream:

- `BridgeEmulator/configManager/argumentHandler.py` — rewrite hard-coded
  `/opt/hue-emulator/...` paths, add `--version`, `--cert-path`,
  `--no-cert-gen`.
- `BridgeEmulator/HueEmulator3.py` — `runHttps` reads `CERT_PATH`; wrap
  `app.wsgi_app` in `werkzeug.middleware.proxy_fix.ProxyFix`.
- `BridgeEmulator/genCert.sh` — rewrite `/opt/hue-emulator/openssl.conf`,
  emit private key + public cert into the caller-supplied config dir
  instead of cwd.

Two patches were dropped once the same fixes landed in the source tree:
`homeAssistantWS.create_ws_client` now defaults its optional config values
(no more `UnboundLocalError: use_https`), and `logManager/logger.py` honours
`DIYHUE_LOG_FILE` directly so the check phase (cwd=`/`) does not crash
trying to open `./diyhue.log`.
