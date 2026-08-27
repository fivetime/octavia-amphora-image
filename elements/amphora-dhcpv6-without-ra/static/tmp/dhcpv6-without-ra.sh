#!/bin/sh
# Add "solicit DHCPv6 without waiting for a router advertisement" to whatever
# netplan generated for this boot.
#
# The drop-in has to be written at run time: its directory name must match the
# generated unit, and that name carries the interface name (ens2, ens3, ...),
# which is not known until the instance boots.
set -eu

DIR=/run/systemd/network

# cloud-init writes the netplan YAML during the local stage and netplan's
# generator turns it into .network files; on first boot that can land just
# after this service is ordered to run. Wait a bounded time rather than
# racing it -- and if nothing ever appears, leave the boot alone.
i=0
while [ "$i" -lt 30 ]; do
    if [ -n "$(find "$DIR" -maxdepth 1 -name '*.network' -print -quit 2>/dev/null)" ]; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

found=0
for f in "$DIR"/*.network; do
    [ -e "$f" ] || continue
    d="$DIR/$(basename "$f").d"
    mkdir -p "$d"
    printf '[DHCPv6]\nWithoutRA=solicit\n' > "$d/10-without-ra.conf"
    found=1
done

if [ "$found" = 1 ]; then
    # networkctl reload re-reads .network files without touching live links.
    networkctl reload 2>/dev/null || systemctl reload systemd-networkd 2>/dev/null || true
fi
