#!/bin/sh
# pre-init-uid-guard.sh — validate the runtime UID before s6-overlay /init.
#
# s6-overlay's s6-rc-compile runs with the container's real UID and cannot
# read its service definitions when the container starts with an arbitrary
# --user value. Reject that configuration before /init starts and emit the
# same guidance as main-wrapper.sh.
#
# Root (0) and the image's hermes user (10000 before any runtime remap) are
# supported. exec preserves PID 1 for /init after this one-time validation.
cur_uid="$(id -u)"
hermes_uid="$(id -u hermes 2>/dev/null || echo 10000)"

if [ "$cur_uid" != 0 ] && [ "$cur_uid" != "$hermes_uid" ]; then
    cat >&2 <<EOF
[hermes] ERROR: container started with --user $cur_uid (an arbitrary, non-hermes UID) — not supported.

To make container-written files match your HOST user, don't use --user.
Start as root (the default) and pass your host UID/GID instead:

    docker run -e HERMES_UID=\$(id -u) -e HERMES_GID=\$(id -g) ...

NAS users (Synology / unRAID / UGOS) can use the PUID/PGID aliases:

    docker run -e PUID=\$(id -u) -e PGID=\$(id -g) ...

The image remaps the hermes user to that UID/GID at boot and chowns the data
volume, so files land owned by your host user — the same outcome --user gave,
without breaking the s6 supervision tree.
EOF
    exit 1
fi

exec /init /opt/hermes/docker/main-wrapper.sh "$@"
