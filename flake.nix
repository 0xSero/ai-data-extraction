{
  description = "A Nix-flake-based Python development environment";

  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0.1"; # unstable Nixpkgs

  outputs =
    { self, ... }@inputs:

    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forEachSupportedSystem =
        f:
        inputs.nixpkgs.lib.genAttrs supportedSystems (
          system:
          f {
            pkgs = import inputs.nixpkgs { inherit system; };
          }
        );

      /*
        Change this value ({major}.{min}) to
        update the Python virtual-environment
        version. When you do this, make sure
        to delete the `.venv` directory to
        have the hook rebuild it for the new
        version, since it won't overwrite an
        existing one. After this, reload the
        development shell to rebuild it.
        You'll see a warning asking you to
        do this when version mismatches are
        present. For safety, removal should
        be a manual step, even if trivial.
      */
      version = "3.13";

      mkPerSystem =
        { pkgs }:
        let
          concatMajorMinor =
            v:
            pkgs.lib.pipe v [
              pkgs.lib.versions.splitVersion
              (pkgs.lib.sublist 0 2)
              pkgs.lib.concatStrings
            ];

          python = pkgs."python${concatMajorMinor version}";

          extractorSpecs = [
            {
              key = "claude-code";
              binary = "extract-claude-code";
              script = "extract_claude_code.py";
            }
            {
              key = "codex";
              binary = "extract-codex";
              script = "extract_codex.py";
            }
            {
              key = "continue";
              binary = "extract-continue";
              script = "extract_continue.py";
            }
            {
              key = "cursor";
              binary = "extract-cursor";
              script = "extract_cursor.py";
            }
            {
              key = "gemini";
              binary = "extract-gemini";
              script = "extract_gemini.py";
            }
            {
              key = "opencode";
              binary = "extract-opencode";
              script = "extract_opencode.py";
            }
            {
              key = "trae";
              binary = "extract-trae";
              script = "extract_trae.py";
            }
            {
              key = "windsurf";
              binary = "extract-windsurf";
              script = "extract_windsurf.py";
            }
          ];

          mkExtractorPackage =
            spec:
            pkgs.writeShellApplication {
              name = spec.binary;
              runtimeInputs = [ python ];
              text = ''
                exec ${python}/bin/python ${self}/${spec.script} "$@"
              '';
              meta.mainProgram = spec.binary;
            };

          extractorPackages = builtins.listToAttrs (
            builtins.map (spec: {
              name = spec.key;
              value = mkExtractorPackage spec;
            }) extractorSpecs
          );

          extractorList = pkgs.lib.concatStringsSep "\n" (
            builtins.map (spec: spec.key) extractorSpecs
          );

          extractorDispatch = pkgs.lib.concatStringsSep "\n" (
            builtins.map (
              spec:
              ''
                ${spec.key})
                  shift
                  exec ${python}/bin/python ${self}/${spec.script} "$@"
                  ;;
              ''
            ) extractorSpecs
          );

          cli = pkgs.writeShellApplication {
            name = "ai-data-extraction";
            runtimeInputs = [ python ];
            text = ''
              show_help() {
                cat <<'EOF'
              Usage: ai-data-extraction <extractor|all|list|help>

              Extractors:
              ${extractorList}

              Examples:
                ai-data-extraction list
                ai-data-extraction cursor
                ai-data-extraction all
                nix run .#cursor
              EOF
              }

              run_extractor() {
                case "$1" in
              ${extractorDispatch}
                  *)
                    printf 'Unknown extractor: %s\n\n' "$1" >&2
                    show_help >&2
                    exit 1
                    ;;
                esac
              }

              command="''${1:-help}"

              case "$command" in
                help|-h|--help)
                  show_help
                  ;;
                list)
                  printf '%s\n' ${pkgs.lib.escapeShellArg extractorList}
                  ;;
                all)
                  shift
                  for extractor in ${pkgs.lib.escapeShellArgs (builtins.map (spec: spec.key) extractorSpecs)}; do
                    printf '==> %s\n' "$extractor"
                    ${pkgs.lib.getExe python} ${self}/extract_''${extractor//-/_}.py "$@"
                  done
                  ;;
                *)
                  run_extractor "$@"
                  ;;
              esac
            '';
            meta.mainProgram = "ai-data-extraction";
          };

          apps = builtins.listToAttrs (
            builtins.map (spec: {
              name = spec.key;
              value = {
                type = "app";
                program = "${extractorPackages.${spec.key}}/bin/${spec.binary}";
              };
            }) extractorSpecs
          );
        in
        {
          packages = extractorPackages // {
            default = cli;
            ai-data-extraction = cli;
          };

          apps = apps // {
            default = {
              type = "app";
              program = "${cli}/bin/ai-data-extraction";
            };
            ai-data-extraction = {
              type = "app";
              program = "${cli}/bin/ai-data-extraction";
            };
          };

          devShells = {
            default = pkgs.mkShellNoCC {
              venvDir = ".venv";

              postShellHook = ''
                venvVersionWarn() {
                	local venvVersion
                	venvVersion="$("$venvDir/bin/python" -c 'import platform; print(platform.python_version())')"

                	[[ "$venvVersion" == "${python.version}" ]] && return

                	cat <<EOF
              Warning: Python version mismatch: [$venvVersion (venv)] != [${python.version}]
                       Delete '$venvDir' and reload to rebuild for version ${python.version}
              EOF
                }

                venvVersionWarn
              '';

              packages = with python.pkgs; [
                venvShellHook
                pip

                # Add whatever else you'd like here.
                # pkgs.basedpyright

                # pkgs.black
                # or
                # python.pkgs.black

                # pkgs.ruff
                # or
                # python.pkgs.ruff
              ];
            };
          };
        };
    in
    {
      packages = forEachSupportedSystem ({ pkgs }: (mkPerSystem { inherit pkgs; }).packages);
      apps = forEachSupportedSystem ({ pkgs }: (mkPerSystem { inherit pkgs; }).apps);
      devShells = forEachSupportedSystem ({ pkgs }: (mkPerSystem { inherit pkgs; }).devShells);
    };
}
