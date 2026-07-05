#! /bin/bash
# AppImage entry point. Runs the app through the bundled Python as a module
# (`python -m phidler`) rather than via the `phidler` console script: pip rewrites
# a console script's shebang to the build-time interpreter path, which is wrong
# once the AppImage is mounted somewhere else at runtime. `{{python-executable}}`
# is substituted by python-appimage with $APPDIR/usr/bin/pythonX.Y.
"{{python-executable}}" -m phidler "$@"
