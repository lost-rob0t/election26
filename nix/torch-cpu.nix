# Official, hash-pinned PyTorch CPU wheel. Never substitute the CUDA torch-bin.
# Index: https://download.pytorch.org/whl/cpu/torch/
# Verified wheel SHA256: 4ca4a9394b0c771238a4f73590fdbbc4debad85ed0fa63d026ae1b085da7d6e2
# Published METADATA SHA256: b3c7f4b3f77d06f86a42aab7d0facef7b661ca3c22967bfcc396917d935dd335
{ pkgs, pythonPackages }:
assert pkgs.stdenv.hostPlatform.system == "x86_64-linux";
assert pythonPackages.python.pythonVersion == "3.12";
pythonPackages.buildPythonPackage {
  pname = "torch";
  version = "2.13.0+cpu";
  format = "wheel";
  src = pkgs.fetchurl {
    name = "torch-2.13.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl";
    url = "https://download.pytorch.org/whl/cpu/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl";
    hash = "sha256-TKSpOUsMdxI4pPc1kP27xN662F7Q+mPQJq4bCF2n1uI=";
  };
  nativeBuildInputs = [ pkgs.autoPatchelfHook ];
  buildInputs = [ pkgs.stdenv.cc.cc.lib ];
  # Exact mandatory requirements from the hash-verified wheel METADATA.
  dependencies = with pythonPackages; [
    filelock typing-extensions setuptools sympy networkx jinja2 fsspec
  ];
  preFixup = ''
    addAutoPatchelfSearchPath "$out/${pythonPackages.python.sitePackages}/torch/lib"
  '';
  pythonImportsCheck = [ "torch" ];
  # Preserve all upstream license files in the wheel. This SPDX list is from
  # its verified METADATA; no CUDA or Intel Simplified License override is used.
  meta = {
    description = "Official PyTorch CPU-only wheel for election26 local Whisper";
    homepage = "https://pytorch.org/";
    license = with pkgs.lib.licenses; [ asl20 llvm-exception bsd2 bsd3 boost mit ];
    sourceProvenance = [ pkgs.lib.sourceTypes.binaryNativeCode ];
    platforms = [ "x86_64-linux" ];
  };
}
