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
    substituteInPlace BridgeEmulator/configManager/argumentHandler.py \
      --replace-fail '/bin/bash' '${bash}/bin/bash' \
      --replace-fail '/opt/hue-emulator/genCert.sh' "$out/share/diyhue/genCert.sh"

    # Inject --version flag (action="version") so versionCheckHook has something to match.
    sed -i '/ap = argparse.ArgumentParser()/a\    ap.add_argument("--version", action="version", version="${finalAttrs.version}")' \
      BridgeEmulator/configManager/argumentHandler.py

    substituteInPlace BridgeEmulator/genCert.sh \
      --replace-fail '/opt/hue-emulator/openssl.conf' "$out/share/diyhue/openssl.conf" \
      --replace-fail 'private.key' '$config/private.key' \
      --replace-fail 'public.crt' '$config/public.crt'

    # Allow overriding the log file path so check phases (which run with cwd=/) don't crash.
    substituteInPlace BridgeEmulator/logManager/logger.py \
      --replace-fail "filename='diyhue.log'" \
        "filename=__import__('os').environ.get('DIYHUE_LOG_FILE', 'diyhue.log')"
  '';

  installPhase = ''
    runHook preInstall

    install -d $out/share/diyhue
    cp -r BridgeEmulator/. $out/share/diyhue/

    tmp=$(mktemp -d)
    unzip -q ${diyHueUI} -d "$tmp"
    install -Dm644 "$tmp/dist/index.html" $out/share/diyhue/flaskUI/templates/index.html
    cp -r "$tmp/dist/assets" $out/share/diyhue/flaskUI/assets

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
    for bin in $out/bin/*; do
      echo "Smoke-testing $(basename "$bin") --help"
      "$bin" --help > /dev/null
    done
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
