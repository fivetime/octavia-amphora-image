=========================
amphora-dhcpv6-without-ra
=========================

Lets the amphora obtain its management address on a network that emits no
router advertisements.

Why
---

cloud-init renders the management interface from the config drive as
``dhcp6: true``. systemd-networkd does not start the DHCPv6 client on that
alone: by default it waits for a router advertisement with the M flag, and an
*isolated* management network has none. In OVN, router advertisements come
only from a logical router port, so a management network with no router
attached -- which is what the security model wants, see the Octavia
documentation on the lb-mgmt-net -- never emits one. The interface then comes
up with no IPv6 address, ``systemd-networkd-wait-online`` times out after two
minutes, and the controller's connectivity check to port 9443 fails with a
timeout that names neither the interface nor the cause.

Verified on 2026-08-27: attaching a router to such a network made the same
amphora reachable on IPv6 immediately, and detaching it broke it again. The
amphora agent itself is fine -- ``[haproxy_amphora] bind_host`` already
defaults to ``::``.

What it does
------------

Installs a one-shot service, ordered before
``systemd-networkd-wait-online.service``, that writes

.. code-block:: ini

   [DHCPv6]
   WithoutRA=solicit

as a drop-in for every ``.network`` file netplan generated, then reloads
networkd.

Why a drop-in written at run time rather than a plain ``.network`` file:
networkd applies exactly **one** ``.network`` per link, the first match in
lexical order, and netplan's is ``10-netplan-<id>.network``. A file of our own
would either be ignored (if numbered higher) or would replace netplan's
configuration wholesale (if numbered lower). Drop-ins are the supported way to
add to it, and their directory name has to match the generated file -- whose
name carries the interface name, which is not known until the instance boots.

This only affects networks with no router advertisement. Where an RA is
present the DHCPv6 client would have started anyway, and ``solicit`` is what
it would have done.
