{
  lib,
  stdenv,
  fetchurl,
  makeWrapper,
  python3,
  unzip,
  openssl,
  libfaketime,
  bash,
  iproute2,
  coreutils,
  versionCheckHook,
}:
let
  pythonEnv = python3.withPackages (
    ps: with ps; [
      astral
      ws4py
      requests
      paho-mqtt
      email-validator
      flask
      flask-login
      flask-restful
      flask-wtf
      werkzeug
      wtforms
      pyyaml
      zeroconf
      flask-cors
      yeelight
      python-kasa
      bleak
      rgbxy
    ]
  );

  diyHueUI = fetchurl {
    url = "https://github.com/diyhue/diyHueUI/releases/download/v2.0.3/DiyHueUI-release.zip";
    hash = "sha256-vKh5mtnozKem5sIP/hOAD02oGdduc0YE2XEATdFcWaM=";
  };
in
stdenv.mkDerivation (finalAttrs: {
  pname = "diyhue";
  version = "0-unstable-2026-06-16";

  src = lib.cleanSource ../.;

  nativeBuildInputs = [
    makeWrapper
    unzip
    versionCheckHook
  ];

  dontConfigure = true;
  dontBuild = true;

  postPatch = ''
    # ---- argumentHandler.py: replace hard-coded /opt paths and add CLI flags ----
    substituteInPlace BridgeEmulator/configManager/argumentHandler.py \
      --replace-fail '/bin/bash' '${bash}/bin/bash' \
      --replace-fail '/opt/hue-emulator/genCert.sh' "$out/share/diyhue/genCert.sh"

    # Inject --version (for versionCheckHook), --cert-path (custom cert location)
    # and --no-cert-gen (skip startup cert generation entirely). Inserting after
    # the argparse constructor keeps the patch idempotent.
    sed -i '/ap = argparse.ArgumentParser()/a\
    ap.add_argument("--version", action="version", version="${finalAttrs.version}")\
    ap.add_argument("--cert-path", help="Path to cert.pem (default: <config_path>/cert.pem)", type=str)\
    ap.add_argument("--no-cert-gen", action="store_true", help="Do not generate cert at startup; expect one at --cert-path")' \
      BridgeEmulator/configManager/argumentHandler.py

    # Honour the new flags in the runtime arg dict and short-circuit the
    # generate_certificate() call when --no-cert-gen is set.
    substituteInPlace BridgeEmulator/configManager/argumentHandler.py \
      --replace-fail \
        'if not path.isfile(configDir + "/cert.pem"):
        generate_certificate(args["MAC"], configDir)' \
        'cert_path = args.get("CERT_PATH") or (configDir + "/cert.pem")
    if not args.get("NO_CERT_GEN") and not path.isfile(cert_path):
        generate_certificate(args["MAC"], path.dirname(cert_path))
    args["CERT_PATH"] = cert_path'

    # Persist the new args into the dict returned by parse_arguments() so
    # downstream code (HueEmulator3.py) can read them out of runtimeConfig.
    substituteInPlace BridgeEmulator/configManager/argumentHandler.py \
      --replace-fail \
        'argumentDict["MAC"] = mac
    argumentDict["DOCKER"] = docker' \
        'argumentDict["MAC"] = mac
    argumentDict["DOCKER"] = docker
    argumentDict["CERT_PATH"] = args.cert_path
    argumentDict["NO_CERT_GEN"] = args.no_cert_gen'

    # ---- HueEmulator3.py: use the configured cert path + trust reverse proxies ----
    substituteInPlace BridgeEmulator/HueEmulator3.py \
      --replace-fail \
        'def runHttps(BIND_IP, HOST_HTTPS_PORT, CONFIG_PATH):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=CONFIG_PATH + "/cert.pem")' \
        'def runHttps(BIND_IP, HOST_HTTPS_PORT, CERT_PATH):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=CERT_PATH)' \
      --replace-fail \
        'if not DISABLE_HTTPS:
        Thread(target=runHttps, args=[BIND_IP, HOST_HTTPS_PORT, CONFIG_PATH]).start()' \
        'if not DISABLE_HTTPS:
        Thread(target=runHttps, args=[BIND_IP, HOST_HTTPS_PORT, configManager.runtimeConfig.arg["CERT_PATH"]]).start()'

    # Trust X-Forwarded-For / X-Real-IP from a single trusted proxy hop so that
    # the request.remote_addr-based loopback bypass and the apiUser.last_use_date
    # accounting in flaskUI/restful.py do not treat every reverse-proxied client
    # as 127.0.0.1.
    substituteInPlace BridgeEmulator/HueEmulator3.py \
      --replace-fail \
        'from werkzeug.serving import WSGIRequestHandler' \
        'from werkzeug.serving import WSGIRequestHandler
from werkzeug.middleware.proxy_fix import ProxyFix' \
      --replace-fail \
        'app = Flask(__name__, template_folder='"'"'flaskUI/templates'"'"',static_url_path="/assets", static_folder='"'"'flaskUI/assets'"'"')' \
        'app = Flask(__name__, template_folder='"'"'flaskUI/templates'"'"',static_url_path="/assets", static_folder='"'"'flaskUI/assets'"'"')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)'

    # ---- homeAssistantWS.py: initialise locals so a missing optional config
    # key does not raise UnboundLocalError at startup. ----
    substituteInPlace BridgeEmulator/services/homeAssistantWS.py \
      --replace-fail \
        'def create_ws_client(bridgeConfig):
    global homeassistant_token
    global homeassistant_url
    global include_by_default
    if '"'"'homeAssistantIp'"'"' in bridgeConfig["config"]["homeassistant"]:' \
        'def create_ws_client(bridgeConfig):
    global homeassistant_token
    global homeassistant_url
    global include_by_default
    homeassistant_ip = "127.0.0.1"
    homeAssistant_port = 8123
    use_https = False
    if '"'"'homeAssistantIp'"'"' in bridgeConfig["config"]["homeassistant"]:'

    # ---- genCert.sh: replace hard-coded openssl.conf path and emit cert into $config ----
    substituteInPlace BridgeEmulator/genCert.sh \
      --replace-fail '/opt/hue-emulator/openssl.conf' "$out/share/diyhue/openssl.conf" \
      --replace-fail 'private.key' '$config/private.key' \
      --replace-fail 'public.crt' '$config/public.crt'
  '';

  installPhase = ''
    runHook preInstall

    install -d $out/share/diyhue
    cp -r BridgeEmulator/. $out/share/diyhue/

    tmp=$(mktemp -d)
    unzip -q ${diyHueUI} -d "$tmp"
    install -Dm644 "$tmp/dist/index.html" $out/share/diyhue/flaskUI/templates/index.html
    # Merge the Vite build's hashed bundles *into* the existing assets dir (which
    # already ships images/, login.css, ... referenced by index.html and the
    # login template). Copying the directory itself would nest them at
    # flaskUI/assets/assets/, so Flask's /assets static route would 404 every
    # index-*.js / index-*.css and the SPA module load would fail.
    cp -r "$tmp/dist/assets/." $out/share/diyhue/flaskUI/assets/

    install -d $out/bin
    makeWrapper ${pythonEnv}/bin/python3 $out/bin/diyhue \
      --add-flags "$out/share/diyhue/HueEmulator3.py" \
      --prefix PATH : ${
        lib.makeBinPath [
          pythonEnv
          openssl
          libfaketime
          bash
          iproute2
        ]
      }

    # Stand-alone wrapper around genCert.sh so it can be invoked outside the
    # diyhue process (e.g. by a systemd oneshot that primes the cert before
    # nginx starts). PATH carries everything genCert.sh actually shells out to.
    makeWrapper ${bash}/bin/bash $out/bin/diyhue-genCert \
      --add-flags "$out/share/diyhue/genCert.sh" \
      --prefix PATH : ${
        lib.makeBinPath [
          pythonEnv
          openssl
          libfaketime
          bash
          coreutils
        ]
      }

    runHook postInstall
  '';

  doInstallCheck = true;
  versionCheckProgramArg = "--version";
  # versionCheckHook runs the binary with --ignore-environment; pass DIYHUE_LOG_FILE through.
  versionCheckKeepEnvironment = [ "DIYHUE_LOG_FILE" ];
  env.DIYHUE_LOG_FILE = "/tmp/diyhue.log";
  installCheckPhase = ''
    runHook preInstallCheck
    cd "$TMPDIR"
    # Only smoke-test the main entrypoint; diyhue-genCert needs a MAC argument and
    # writes to disk, which the sandbox isn't set up for.
    echo "Smoke-testing diyhue --help"
    $out/bin/diyhue --help > /dev/null
    runHook postInstallCheck
  '';

  passthru = {
    inherit pythonEnv diyHueUI;
  };

  meta = {
    description = "Philips Hue Bridge emulator (no upstream test suite — see flake checks.vmTest)";
    homepage = "https://diyhue.org/";
    license = lib.licenses.mit;
    mainProgram = "diyhue";
    platforms = lib.platforms.linux;
  };
})
