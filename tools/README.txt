tools/ -- vendored service wrapper
==================================

The spec (Appendix B1) asks for nssm.exe to be committed here so that deploying
the system years from now never depends on an external download still being
available.

>>> nssm.exe is NOT included in this commit. <<<

It is a third-party binary (https://nssm.cc, public domain) and must be added by
whoever sets up the repository -- a code-generation tool cannot fabricate a real
executable. To vendor it properly:

  1. Download NSSM from https://nssm.cc/download (e.g. nssm-2.24.zip).
  2. From the zip, copy the 64-bit build  win64\nssm.exe  into this folder, so
     the path is:   tools\nssm.exe
  3. Commit it:     git add -f tools/nssm.exe   (the .gitignore does not exclude
                    it; it is committed on purpose, unlike data files).

Once tools\nssm.exe exists, setup.ps1 -InstallService and update.ps1 will use it
to install/restart the auto-start "BayTracker" Windows service.

Don't want NSSM? You can instead run the server as a Task Scheduler "At startup"
task (see README.md, "Run it as a service"). NSSM is recommended because it also
auto-restarts the app if it ever crashes.
