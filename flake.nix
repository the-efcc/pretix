{
  description = "the-efcc's pretix fork: upstream pretix + our patches";

  inputs = {
    # When consuming this flake from a NixOS configuration, add
    #   inputs.pretix-fork.inputs.nixpkgs.follows = "nixpkgs";
    # so the package builds against the same nixpkgs as the system.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      overlays.default = final: prev: {
        pretix = final.callPackage ./nix/package.nix {
          src = self;
          # The derivation needs a reference to itself: python.pkgs.pretix is
          # the package as an importable Python module (the NixOS module's
          # buildEnv relies on this). Tie the knot through the fixpoint.
          pretix = final.pretix;
        };
      };

      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ self.overlays.default ];
          };
        in
        {
          pretix = pkgs.pretix;
          default = pkgs.pretix;
        }
      );
    };
}
