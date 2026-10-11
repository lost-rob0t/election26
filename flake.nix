{
  description = "Local-first election26 tools; public code only, private runtime state stays outside Nix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/c59305bab2065cfecc4944690d9eedbb56f3a9fa";
    starintel-doc = {
      url = "github:lost-rob0t/starintel-doc/6aa8051dc7ea560943a60a25e1d600467f2778bc";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, starintel-doc }:
    let
      # First verified target. Do not infer Android, ARM64 or Darwin support.
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = pkgs.lib;
      python = pkgs.python312;
      ps = pkgs.python312Packages;
      source = import ./nix/source.nix { inherit lib; root = ./.; };
      # Reuse the producer package unchanged apart from an honest Nix receipt.
      # PyPA wheel installs do not create pip's VCS direct_url.json metadata.
      sdk = assert starintel-doc.rev == "6aa8051dc7ea560943a60a25e1d600467f2778bc";
        starintel-doc.packages.${system}.starintel-doc.overridePythonAttrs (old: {
          postInstall = (old.postInstall or "") + ''
            info="$out/${python.sitePackages}/starintel_doc-${old.version}.dist-info"
            test -d "$info"
            cp ${pkgs.writeText "starintel-doc-nix-source.json" (builtins.toJSON {
              format = "election26-nix-source-v1";
              source_url = "github:lost-rob0t/starintel-doc";
              source_rev = starintel-doc.rev;
              source_nar_hash = starintel-doc.narHash;
            })} "$info/election26_nix_source.json"
          '';
        });
      mkElection = { withSDK ? false, withKeyring ? false, withWhisper ? false }:
        ps.buildPythonPackage {
          pname = "election26";
          version = "0.1.0";
          pyproject = true;
          src = source.src;
          build-system = [ ps.setuptools ];
          dependencies = lib.optional withSDK sdk
            ++ lib.optional withKeyring ps.keyring
            ++ lib.optional withWhisper ps.openai-whisper;
          nativeCheckInputs = [ pkgs.bash ];
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            export ELECTION26_TEST_STARINTEL=${if withSDK then "1" else "0"}
            python -m unittest discover -s tests -v
            runHook postCheck
          '';
          postInstall = ''
            install -Dm755 scripts/cron.py "$out/bin/election26-cron"
            substituteInPlace "$out/bin/election26-cron" \
              --replace-fail '#!/usr/bin/env python3' '#!${python.interpreter}'
          '';
          pythonImportsCheck = [ "election26" ]
            ++ lib.optional withSDK "starintel_doc"
            ++ lib.optional withKeyring "keyring"
            ++ lib.optional withWhisper "whisper";
          meta = {
            description = "Bounded public-source archive CLI with explicit local runtime configuration";
            homepage = "https://github.com/lost-rob0t/election26";
            mainProgram = "election26";
            platforms = [ system ];
            # The repository has no explicit root license. Do not invent one.
          };
        };
      core = mkElection {};
      withSDK = mkElection { withSDK = true; };
      withKeyring = mkElection { withKeyring = true; };
      withWhisper = mkElection { withWhisper = true; };
      full = mkElection { withSDK = true; withKeyring = true; withWhisper = true; };
      mediaTools = pkgs.symlinkJoin {
        name = "election26-media-tools";
        paths = [ pkgs.yt-dlp pkgs.ffmpeg ];
      };
      smoke = name: package: flags:
        let environment = python.withPackages (_: [ package ]);
        in pkgs.runCommand name { nativeBuildInputs = [ environment ]; } ''
          export HOME="$TMPDIR/home"
          mkdir -p "$HOME"
          ${environment}/bin/python ${./scripts/check-installed.py} \
            --bin ${package}/bin --python ${environment}/bin/python ${flags}
          touch "$out"
        '';
      app = package: command: { type = "app"; program = "${package}/bin/${command}"; };
    in {
      packages.${system} = {
        default = core;
        election26 = core;
        election26-starintel = withSDK;
        election26-keyring = withKeyring;
        election26-whisper = withWhisper;
        election26-full = full;
        cron = core;
        fec-target = core;
        starintel-doc = sdk;
        keyring = ps.keyring;
        whisper = ps.openai-whisper;
        yt-dlp = pkgs.yt-dlp;
        ffmpeg = pkgs.ffmpeg;
        media-tools = mediaTools;
      };
      apps.${system} = {
        default = app core "election26";
        election26 = app core "election26";
        election26-starintel = app withSDK "election26";
        election26-keyring = app withKeyring "election26";
        election26-whisper = app withWhisper "election26";
        election26-full = app full "election26";
        cron = app core "election26-cron";
        fec-target = app core "election26-fec-target";
        keyring = app ps.keyring "keyring";
        whisper = app ps.openai-whisper "whisper";
        yt-dlp = app pkgs.yt-dlp "yt-dlp";
        ffmpeg = app pkgs.ffmpeg "ffmpeg";
      };
      checks.${system} = {
        core = smoke "election26-core-installed" core "";
        canonical-sdk = smoke "election26-sdk-installed" withSDK "--with-sdk";
        keyring = smoke "election26-keyring-installed" withKeyring "--with-keyring --keyring-bin ${ps.keyring}/bin";
        whisper = smoke "election26-whisper-installed" withWhisper "--with-whisper --whisper-bin ${ps.openai-whisper}/bin";
        full = smoke "election26-full-installed" full
          "--with-sdk --with-keyring --with-whisper --media-bin ${mediaTools}/bin --keyring-bin ${ps.keyring}/bin --whisper-bin ${ps.openai-whisper}/bin";
        source-boundary =
          assert lib.all (path: !(source.allowed path "regular")) [
            "config.local.json" "config/private.json" ".env" "credentials.json"
            "archive/catalog.sqlite" "routes/fixture.kml" "wigle/input.kmz"
            "src/election26/private.py" "tests/raw.json" "src/election26/__pycache__/cli.pyc"
          ];
          assert !(source.allowed "src/election26/cli.py" "symlink");
          pkgs.runCommand "election26-source-boundary" { nativeBuildInputs = [ python ]; } ''
            python - ${source.src} ${./nix/source-files.json} <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
expected = set(json.loads(Path(sys.argv[2]).read_text()))
actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
assert actual == expected, (actual - expected, expected - actual)
assert not any(path.is_symlink() for path in root.rglob("*"))
print("Exact public build-source allowlist verified")
PY
            touch "$out"
          '';
      };
      devShells.${system} = {
        default = pkgs.mkShell {
          packages = [ (python.withPackages (_: [ core ps.pip ps.setuptools ps.wheel ])) pkgs.git pkgs.bash ];
          # No hooks, credentials, generated config, downloads or schedules.
        };
        full = pkgs.mkShell {
          packages = [ (python.withPackages (_: [ full ps.pip ps.setuptools ps.wheel ])) mediaTools pkgs.git pkgs.bash ];
        };
      };
    };
}
