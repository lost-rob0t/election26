# Exact source allowlist: no recursive directory inclusion, secrets or raw data.
# This filters derivation inputs. A local flake root itself is copied by Nix
# before evaluation, so private data must never be placed in that checkout.
{ lib, root }:
let
  files = builtins.fromJSON (builtins.readFile ./source-files.json);
  directories = lib.unique (lib.concatMap (file:
    let parts = lib.splitString "/" file;
    in map (count: lib.concatStringsSep "/" (lib.take count parts))
      (lib.range 1 ((builtins.length parts) - 1))
  ) files);
  allowed = relative: type:
    (type == "regular" && builtins.elem relative files)
    || (type == "directory" && builtins.elem relative directories);
  src = builtins.path {
    path = root;
    name = "election26-public-source";
    filter = path: type: (toString path == toString root && type == "directory")
      || allowed (lib.removePrefix (toString root + "/") (toString path)) type;
  };
in { inherit files allowed src; }
