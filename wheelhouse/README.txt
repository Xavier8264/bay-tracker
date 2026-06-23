Vendored Python wheels (offline / air-gapped install)
=====================================================

These .whl files are the EXACT pinned dependencies from ..\requirements.txt,
committed into the repo on purpose -- the same principle as tools\nssm.exe
(spec Appendix B1): deployment must never depend on an external download being
available years from now.

They let setup.ps1 install everything with NO internet:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Offline

setup.ps1 also auto-detects a missing network and falls back to these wheels,
so a normal online install is unchanged.

The wheels are built for Windows 64-bit / Python 3.14 (the deployment target).
To refresh them after changing requirements.txt, run on a CONNECTED machine:

    py -3 -m pip download -r requirements.txt -d wheelhouse

then commit the updated folder.
