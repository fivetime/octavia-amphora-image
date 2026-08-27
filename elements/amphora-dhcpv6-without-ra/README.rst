=========================
amphora-dhcpv6-without-ra
=========================

Lets the amphora reach its management network when nothing on that network
sends router advertisements.

Why
---

An isolated management network has no router advertisements. In OVN they come
only from a logical router port, so a management network with no router
attached -- which is what the security model wants, see the Octavia
documentation on the lb-mgmt-net -- never emits one. Two separate things break
as a result, and fixing only the first is not enough to make the amphora
reachable.

**No address.** cloud-init renders the management interface from the config
drive as ``dhcp6: true``. systemd-networkd does not start the DHCPv6 client on
that alone: by default it waits for an RA with the M flag. The interface comes
up with no IPv6 address, ``systemd-networkd-wait-online`` times out after two
minutes, and the controller's connectivity check to port 9443 fails with a
timeout that names neither the interface nor the cause.

Note that systemd 255 -- noble -- still defaults ``WithoutRA=`` to ``no``,
despite the prose under ``DHCP=`` in the same man page claiming the client
starts "regardless of the presence of routers on the link". The definition of
``WithoutRA=`` is the one that matches observed behaviour.

**No route.** DHCPv6 (IA_NA) conveys an address and no prefix; the on-link
prefix is normally learned from the RA. networkd therefore installs the
address as a ``/128``, and the instance has no route to anything else on the
link -- including the health manager. It answers packets whose reply
destination happens to be on-link and silently drops the rest, which looks
exactly like a host that is down.

Verified on the testbed:

* 2026-08-27, attaching a router to such a network made the amphora reachable
  on IPv6 immediately, and detaching it broke it again.
* 2026-08-28, with the solicit fix alone: the DHCPv6 exchange completes
  (SOLICIT/ADVERTISE/REQUEST/REPLY captured on the tap), the agent listens and
  completes a TLS handshake when reached from a link-local source, and every
  echo request from the health manager's global address arrives at the
  instance and goes unanswered. Address present, route absent.

The amphora agent itself is fine -- ``[haproxy_amphora] bind_host`` already
defaults to ``::``.

What it does
------------

Installs a one-shot service, ordered before
``systemd-networkd-wait-online.service``, that writes a drop-in over netplan's
generated ``.network`` file:

.. code-block:: ini

   [DHCPv6]
   WithoutRA=solicit

   [Route]
   Destination=fc00:f:2:1:1::/108
   Scope=link

then reloads networkd.

The prefix is recovered from the config drive, which carries ``ip_address``
and ``netmask`` even for an ``ipv6_dhcpv6-stateful`` network -- cloud-init's
netplan renderer discards them and writes ``dhcp6: true``. The route is
installed only on the interface whose MAC owns that address, so a management
prefix is never written onto the tenant NIC.

Networks that need neither are left alone: statically configured IPv6 already
carries its prefix from cloud-init, and SLAAC implies an RA exists.

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
