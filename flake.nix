{
  description = "diyHue - Philips Hue Bridge emulator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in
    {
      packages = forAllSystems (pkgs: {
        diyhue = pkgs.callPackage ./nix/package.nix { };
        default = self.packages.${pkgs.system}.diyhue;
      });

      nixosModules = {
        diyhue = import ./nix/module.nix;
        default = self.nixosModules.diyhue;
      };

      checks = forAllSystems (pkgs: {
        unitTests = pkgs.callPackage ./nix/unitTests.nix {
          diyhuePackage = self.packages.${pkgs.system}.diyhue;
        };
        vmTest = pkgs.callPackage ./nix/vmTest.nix {
          diyhueModule = self.nixosModules.diyhue;
          diyhuePackage = self.packages.${pkgs.system}.diyhue;
        };
        vmTestNginx = pkgs.callPackage ./nix/vmTestNginx.nix {
          diyhueModule = self.nixosModules.diyhue;
          diyhuePackage = self.packages.${pkgs.system}.diyhue;
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (self.packages.${pkgs.system}.diyhue.passthru.pythonEnv)
            pkgs.python3Packages.pytest
            pkgs.openssl
            pkgs.libfaketime
          ];
        };
      });
    };
}
