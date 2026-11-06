#!/bin/sh
# Launcher installed as /usr/bin/phidler by the .deb / .rpm. Runs the app as a
# module through the bundled Python, which ignores the user's site-packages so a
# stray ~/.local install can't shadow the bundled stack.
exec /opt/phidler/bin/python3 -s -m phidler "$@"
