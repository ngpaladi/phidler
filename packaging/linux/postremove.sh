#!/bin/sh
# On removal (not upgrade), delete what the package didn't install itself:
# byte-code caches and anything pip-installed into the bundle later.
case "$1" in
    remove|purge|0) rm -rf /opt/phidler ;;
esac
exit 0
