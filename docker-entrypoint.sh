#!/bin/sh
# Run as Unraid's usual nobody:users (99:100) by default so files in appdata stay editable.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA="${DATA_DIR:-/data}"
umask "${UMASK:-002}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA"
    # Only touch files that aren't already owned correctly, so restarts stay fast.
    find "$DATA" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
