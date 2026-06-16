{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.diyhue;

  # diyHue's argumentHandler.py derives the cert serial as
  # `mac[:6] + "fffe" + mac[-6:]` (mac without colons).
  rawMac = lib.replaceStrings [ ":" ] [ "" ] cfg.mac;
  certSerial =
    (builtins.substring 0 6 rawMac) + "fffe" + (builtins.substring 6 6 rawMac);
in
{
  options.services.diyhue = {
    enable = lib.mkEnableOption "diyHue Philips Hue Bridge emulator";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.diyhue;
      defaultText = lib.literalExpression "pkgs.diyhue";
      description = "diyhue package to use.";
    };

    mac = lib.mkOption {
      type = lib.types.str;
      example = "00:11:22:33:44:55";
      description = ''
        MAC address used to derive the emulated bridge's serial and TLS
        certificate. Must be stable across restarts.
      '';
    };

    bindAddress = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Address to bind the HTTP and HTTPS listeners to.";
    };

    advertisedIp = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        IP address advertised via SSDP / mDNS. When null, diyHue auto-detects.
      '';
    };

    httpPort = lib.mkOption {
      type = lib.types.port;
      default = 80;
      description = "HTTP port. Hue API clients expect 80.";
    };

    httpsPort = lib.mkOption {
      type = lib.types.port;
      default = 443;
      description = "HTTPS port. Hue API clients expect 443.";
    };

    noServeHttps = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Disable the HTTPS listener entirely.";
    };

    generateCert = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether to (re)generate the MAC-bound self-signed certificate before
        diyHue starts. When false, diyHue is launched with --no-cert-gen and
        expects an existing certificate at certPath.
      '';
    };

    certPath = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/diyhue/cert.pem";
      description = ''
        Path to cert.pem (PEM file containing both the private key and the
        public certificate, as produced by diyhue-genCert). diyHue reads this
        for its HTTPS listener and a reverse-proxy can be pointed at the same
        path.
      '';
    };

    certGroup = lib.mkOption {
      type = lib.types.str;
      default = "diyhue";
      description = ''
        Group that owns the generated cert.pem. Add a reverse proxy's user to
        this group so it can read the certificate.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Open ports for HTTP, HTTPS, SSDP (UDP 1900), Yeelight discovery
        (UDP 1982) and Hue entertainment (UDP 2100).
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra command-line arguments passed to HueEmulator3.py.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.diyhue = {
      isSystemUser = true;
      group = "diyhue";
      description = "diyHue service user";
    };
    users.groups.diyhue = { };

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [
        cfg.httpPort
        cfg.httpsPort
      ];
      allowedUDPPorts = [
        1900
        1982
        2100
      ];
    };

    # Oneshot that primes the cert before diyhue (and any reverse proxy) needs
    # it. Runs as root with no sandboxing so it can create the cert directory
    # before diyhue's StateDirectory has materialised. Skipped when
    # generateCert is false.
    systemd.services.diyhue-cert = lib.mkIf cfg.generateCert {
      description = "Generate diyHue MAC-bound TLS certificate";
      wantedBy = [ "multi-user.target" ];
      before = [ "diyhue.service" ];

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };

      script = ''
        set -eu
        certDir="$(dirname '${cfg.certPath}')"
        install -d -o diyhue -g ${cfg.certGroup} -m 0750 "$certDir"
        if [ ! -s '${cfg.certPath}' ]; then
          ${cfg.package}/bin/diyhue-genCert ${certSerial} "$certDir"
          # genCert.sh always writes <config-dir>/cert.pem; move into place if
          # the caller asked for a different filename within the directory.
          if [ "$certDir/cert.pem" != '${cfg.certPath}' ]; then
            mv "$certDir/cert.pem" '${cfg.certPath}'
          fi
        fi
        chown diyhue:${cfg.certGroup} '${cfg.certPath}'
        chmod 0640 '${cfg.certPath}'
      '';
    };

    systemd.services.diyhue = {
      description = "diyHue Philips Hue Bridge emulator";
      wantedBy = [ "multi-user.target" ];
      after =
        [ "network-online.target" ]
        ++ lib.optional cfg.generateCert "diyhue-cert.service";
      wants = [ "network-online.target" ];
      requires = lib.optional cfg.generateCert "diyhue-cert.service";

      serviceConfig = {
        Type = "simple";
        User = "diyhue";
        Group = "diyhue";
        StateDirectory = "diyhue";
        StateDirectoryMode = "0750";
        WorkingDirectory = "/var/lib/diyhue";
        ExecStart = lib.concatStringsSep " " (
          [
            (lib.getExe cfg.package)
            "--mac"
            cfg.mac
            "--config_path"
            "/var/lib/diyhue"
            "--cert-path"
            cfg.certPath
            "--bind-ip"
            cfg.bindAddress
            "--http-port"
            (toString cfg.httpPort)
            "--https-port"
            (toString cfg.httpsPort)
          ]
          ++ lib.optional (!cfg.generateCert) "--no-cert-gen"
          ++ lib.optionals (cfg.advertisedIp != null) [
            "--ip"
            cfg.advertisedIp
          ]
          ++ lib.optional cfg.noServeHttps "--no-serve-https"
          ++ cfg.extraArgs
        );
        Restart = "on-failure";
        RestartSec = 5;

        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
        CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
          "AF_PACKET"
          "AF_UNIX"
        ];
      };
    };
  };
}
