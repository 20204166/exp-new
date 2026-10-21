# Expra Connect Harness

A small Qt application for evaluating Expra Connect as the owner of peer
identity, discovery, pairing, authenticated transport, and cluster membership.
Exp supplies the host data provider and package-facing integration; the window
is a test harness, not a general system-monitoring dashboard.

## Run from source

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
python main.py
```

Connect discovery starts with the runtime. Pairing is explicit and requires
review of both peer fingerprints. **Stop Connect** stops the whole runtime,
including discovery. The harness does not change firewall settings.

## Update Expra Connect

The harness accepts compatible `expra-connect` 2.x releases (`>=2.2.1.1,<3`)
independently of the harness version. To vendor a newer universal Connect wheel
for offline installation, run:

```sh
python -m maintenance._release update-companion \
  /path/to/expra_connect-VERSION-py3-none-any.whl --package-dir .
```

This validates the wheel, updates the supported minimum and bundled companion
artifact/checksums, and preserves the `<3` compatibility boundary. Review the
result and run the release tests before using the updated package. This command
is not run as part of ordinary harness work.

## Build and install release wheels

`install/build.sh` and `install/build.ps1` build the harness as one universal
`py3-none-any` wheel and create a universal release bundle by default. To build
the native variant instead, supply a platform-compatible native Expra Connect
wheel:

```sh
install/build.sh --variant native --native-connect-wheel /path/to/expra_connect-2.2.1.1-cp312-cp312-linux_x86_64.whl
```

PowerShell accepts `-Variant native -NativeConnectWheel <path>`. Run the
universal command first, then the native command; both bundles reuse the exact
same universal harness wheel. Bundles are written to
`dist/universal/<harness-version>/` or `dist/native/<harness-version>/`; each
contains the harness wheel, its selected Connect wheel, and its own
`SHA256SUMS`. The root `dist/` also contains the universal harness wheel, the
universal Connect wheel, any native Connect wheels, and the manifest used by
the online installers. Verify a bundle with
`python -m maintenance._release verify-bundle PATH --variant native|universal`.
Install a local bundle with pip selecting the compatible Connect wheel. Pip may
download the harness's other runtime dependencies from its configured package
index:

```sh
python -m pip install --find-links PATH PATH/system_analyzer-<harness-version>-py3-none-any.whl expra-connect==2.2.1.1
```

The native Connect wheel is used only on its matching CPython/platform; other
supported platforms use the universal Connect wheel. Online installation is
available from this project's own GitHub repository (separate from `exp`):

```sh
curl -fsSL https://raw.githubusercontent.com/20204166/exp-new/main/install/install-online.sh | bash
```

On Windows, run `install/install-online.ps1` from this repository's release.
The local and online install, verify, upgrade, rollback, and uninstall scripts
remain available in `install/`; the per-user desktop launcher remains
available in `install-desktop.sh`. Building never installs the generated wheels.

The installed primary command is `expra-connect-harness`. A legacy console
alias remains for compatibility with the retained installer workflow.

## Development checks

```sh
scripts/run_tests.sh
ruff check .
ruff format --check .
pyright
mypy --ignore-missing-imports
```

Unit tests do not establish physical two-host or LAN acceptance. Do not claim
that acceptance unless it has been performed separately.
