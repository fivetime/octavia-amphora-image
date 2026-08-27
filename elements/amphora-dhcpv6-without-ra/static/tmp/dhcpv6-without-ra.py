#!/usr/bin/python3
"""Make an IPv6 management network usable when nothing sends router
advertisements.

Two things are missing on such a network, and DHCPv6 supplies neither:

1. systemd-networkd will not start its DHCPv6 client at all until it sees an
   RA with the M flag.  ``WithoutRA=solicit`` tells it to solicit anyway.

2. DHCPv6 (IA_NA) conveys an address and no prefix, so networkd installs it as
   a /128.  The on-link prefix normally arrives in the RA.  Without one the
   instance holds its address but has no route to anything else on the link --
   including the health manager -- so replies are silently undeliverable.

The prefix is recovered from the config drive, which carries ``ip_address``
and ``netmask`` even for ``ipv6_dhcpv6-stateful`` networks, and installed as a
scope-link route on the interface that owns that address.

Both are written as drop-ins over netplan's generated ``.network`` file.  A
drop-in rather than a file of our own because networkd applies exactly one
``.network`` per link -- the first match in lexical order -- and netplan's is
``10-netplan-<id>.network``: ours would either be ignored or would replace
netplan's wholesale.  At run time rather than at build time because the
drop-in directory name must match the generated file, whose name carries the
interface name.
"""

import glob
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import time

NETWORK_DIR = "/run/systemd/network"
INSTANCE_DATA = "/run/cloud-init/instance-data.json"
CONFIG_DRIVE_LABEL = "config-2"
NETWORK_DATA_PATH = "openstack/latest/network_data.json"


def log(msg):
    """Say what happened.  An earlier version of this ran, found nothing to do
    and exited 0, which is indistinguishable from success in the journal and
    hid a wholly ineffective boot."""
    print(msg, flush=True)


def prefixlen_from_netmask(netmask):
    """'ffff:ffff:ffff:ffff:ffff:ffff:fff0:0' -> 108."""
    bits = int(ipaddress.IPv6Address(netmask))
    # A netmask is a run of ones then a run of zeros; reject anything else
    # rather than silently installing a route for the wrong prefix.
    plen = 128 - (bits ^ ((1 << 128) - 1)).bit_length()
    if bits != (((1 << plen) - 1) << (128 - plen)):
        raise ValueError("not a contiguous netmask: %s" % netmask)
    return plen


def on_link_prefix(ip_address, netmask):
    """The network the address sits in, as networkd wants it written."""
    plen = prefixlen_from_netmask(netmask)
    net = ipaddress.ip_network("%s/%d" % (ip_address, plen), strict=False)
    return str(net)


def plan_from_network_data(data):
    """Map each interface MAC to the on-link prefix it is missing.

    Only DHCPv6 networks are considered.  A statically configured IPv6
    network already gets its prefix from cloud-init, and a SLAAC one has an RA
    by definition -- in both cases there is nothing here to repair.
    """
    macs = {}
    for link in data.get("links") or []:
        mac = link.get("ethernet_mac_address")
        if link.get("id") and mac:
            macs[link["id"]] = mac.lower()

    plan = {}
    for net in data.get("networks") or []:
        if "dhcpv6" not in (net.get("type") or ""):
            continue
        mac = macs.get(net.get("link"))
        ip, netmask = net.get("ip_address"), net.get("netmask")
        if not (mac and ip and netmask):
            continue
        try:
            plan.setdefault(mac, []).append(on_link_prefix(ip, netmask))
        except ValueError as exc:
            log("  skipping %s: %s" % (ip, exc))
    return plan


def load_network_data():
    """The config drive's network_data.json, however we can reach it."""
    try:
        with open(INSTANCE_DATA) as fh:
            blob = json.load(fh)
        net = (blob.get("ds") or {}).get("network_json")
        if net:
            log("network_data from %s" % INSTANCE_DATA)
            return net
    except Exception as exc:
        log("no usable %s (%s)" % (INSTANCE_DATA, exc))

    dev = "/dev/disk/by-label/%s" % CONFIG_DRIVE_LABEL
    if not os.path.exists(dev):
        log("no config drive labelled %s" % CONFIG_DRIVE_LABEL)
        return None
    mnt = tempfile.mkdtemp()
    try:
        subprocess.run(["mount", "-o", "ro", dev, mnt], check=True,
                       capture_output=True)
    except Exception as exc:
        log("could not mount the config drive: %s" % exc)
        return None
    try:
        with open(os.path.join(mnt, NETWORK_DATA_PATH)) as fh:
            log("network_data from the config drive")
            return json.load(fh)
    except Exception as exc:
        log("config drive carries no readable network_data: %s" % exc)
        return None
    finally:
        subprocess.run(["umount", mnt], capture_output=True)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def mac_of(ifname):
    try:
        with open("/sys/class/net/%s/address" % ifname) as fh:
            return fh.read().strip().lower()
    except OSError:
        return None


def interface_for(network_file):
    """Which interface does this generated .network file govern?"""
    match = {}
    section = None
    try:
        with open(network_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]").lower()
                elif section == "match" and "=" in line:
                    k, v = line.split("=", 1)
                    match[k.strip().lower()] = v.strip()
    except OSError:
        return None, None

    if match.get("macaddress"):
        mac = match["macaddress"].lower()
        for ifname in os.listdir("/sys/class/net"):
            if mac_of(ifname) == mac:
                return ifname, mac
        return None, mac
    if match.get("name"):
        name = match["name"]
        # netplan emits a literal name for the interfaces it renders; a glob
        # would need expanding against the live links.
        if os.path.exists("/sys/class/net/%s" % name):
            return name, mac_of(name)
    return None, None


def wait_for_network_files(timeout=30):
    """cloud-init writes the netplan YAML during the local stage and netplan's
    generator turns it into .network files; on first boot that can land just
    after this service is ordered to run."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = sorted(glob.glob(os.path.join(NETWORK_DIR, "*.network")))
        if files:
            return files
        time.sleep(1)
    return []


def main():
    files = wait_for_network_files()
    if not files:
        log("no .network files appeared in %s after 30s; networkd is not "
            "rendering this instance's configuration, so there is nothing "
            "here to add to" % NETWORK_DIR)
        return 0

    data = load_network_data()
    plan = plan_from_network_data(data or {})
    if data is None:
        log("carrying on without prefix information: DHCPv6 will be "
            "solicited, but an address obtained without a router "
            "advertisement will have no on-link route")
    elif not plan:
        log("no DHCPv6 IPv6 networks in network_data; only the solicit "
            "setting will be written")

    wrote_route = 0
    for path in files:
        ifname, mac = interface_for(path)
        prefixes = plan.get(mac or "", [])
        dropin_dir = "%s.d" % path
        os.makedirs(dropin_dir, exist_ok=True)
        body = ["[DHCPv6]", "WithoutRA=solicit", ""]
        for prefix in prefixes:
            # No Gateway= makes this a device route: the prefix is on-link,
            # which is exactly what the missing RA would have said.
            body += ["[Route]", "Destination=%s" % prefix, "Scope=link", ""]
        with open(os.path.join(dropin_dir, "10-without-ra.conf"), "w") as fh:
            fh.write("\n".join(body))
        log("%s (%s): solicit%s"
            % (os.path.basename(path), ifname or "unmatched",
               "".join(", on-link %s" % p for p in prefixes)))
        wrote_route += len(prefixes)

    if plan and not wrote_route:
        log("WARNING: network_data described %d DHCPv6 interface(s) but none "
            "matched a generated .network file; the on-link route was NOT "
            "installed and the instance will be unreachable off-link"
            % len(plan))

    subprocess.run(["networkctl", "reload"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
