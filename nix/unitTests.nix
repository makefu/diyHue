# Pure (non-VM) test suite for the BridgeEmulator package. Runs the pytest
# suite against the same Python environment the service uses, so a dependency
# that is missing from the package is caught here rather than at boot.
{
  lib,
  runCommand,
  python3Packages,
  diyhuePackage,
}:
runCommand "diyhue-unit-tests"
  {
    nativeBuildInputs = [
      diyhuePackage.passthru.pythonEnv
      python3Packages.pytest
    ];
    src = lib.cleanSource ../BridgeEmulator;
  }
  ''
    cp -r "$src" ./BridgeEmulator
    chmod -R u+w ./BridgeEmulator
    cd ./BridgeEmulator

    # logger.py opens its rotating file handler at import time and the build
    # sandbox has no writable working directory by default.
    export DIYHUE_LOG_FILE="$TMPDIR/diyhue.log"
    export HOME="$TMPDIR"

    pytest -q tests
    touch "$out"
  ''
