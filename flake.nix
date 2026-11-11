{
  description = "Orchard: per-workspace dev containers on Incus, behind one ssh front door";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        orchard = pkgs.python3Packages.buildPythonApplication {
          pname = "orchard";
          version = "0.8.1";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
          meta = {
            description = "Per-workspace dev containers on Incus, behind one ssh front door";
            homepage = "https://github.com/spott/orchard";
            license = pkgs.lib.licenses.mit;
            mainProgram = "dev";
          };
        };
        default = orchard;
      });

      checks = forAll (pkgs: { package = self.packages.${pkgs.stdenv.hostPlatform.system}.default; });

      nixosModules.default = { pkgs, lib, ... }: {
        imports = [ ./nix/module.nix ];
        services.orchard.package = lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (p: [ p.pytest ])) pkgs.ruff ];
        };
      });
    };
}
