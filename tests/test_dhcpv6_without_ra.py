"""Tests for the amphora-dhcpv6-without-ra element's address logic.

The interesting failure this guards against is the quiet one: the element
running, exiting 0, and leaving the instance exactly as it was.  That happened
twice -- once because the hook was not executable and diskimage-builder skipped
it without a word, and once because the element solicited DHCPv6 correctly and
then left the address as a /128 with no route off the link.  Both looked like
success everywhere except from the instance.

The sample below is a real network_data.json, read off the config drive of an
amphora booted on an isolated IPv6 management network on 2026-08-28.
"""

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(
    HERE, os.pardir, "elements", "amphora-dhcpv6-without-ra",
    "static", "tmp", "dhcpv6-without-ra.py")

spec = importlib.util.spec_from_file_location("without_ra", SCRIPT)
without_ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(without_ra)


REAL_SAMPLE = {
    "links": [
        {
            "id": "tap06bf7927-e2",
            "vif_id": "06bf7927-e2d5-418e-877d-bbf1352588d2",
            "type": "ovs",
            "mtu": 1442,
            "ethernet_mac_address": "fa:16:3e:9d:73:42",
        }
    ],
    "networks": [
        {
            "id": "network0",
            "type": "ipv6_dhcpv6-stateful",
            "link": "tap06bf7927-e2",
            "ip_address": "fc00:f:2:1:1::16b",
            "netmask": "ffff:ffff:ffff:ffff:ffff:ffff:fff0:0",
            "routes": [],
            "network_id": "c58388ce-c3ea-4789-8b88-994e8082f398",
            "services": [],
        }
    ],
    "services": [],
}


class TestPrefixLength(unittest.TestCase):

    def test_the_netmask_an_amphora_actually_gets(self):
        self.assertEqual(
            108,
            without_ra.prefixlen_from_netmask(
                "ffff:ffff:ffff:ffff:ffff:ffff:fff0:0"))

    def test_common_lengths(self):
        for netmask, expected in [
                ("ffff:ffff:ffff:ffff::", 64),
                ("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", 128),
                ("::", 0),
                ("ffff:ffff:ffff:ffff:ffff:ffff:ffff:0", 112),
        ]:
            self.assertEqual(expected,
                             without_ra.prefixlen_from_netmask(netmask),
                             netmask)

    def test_a_non_contiguous_mask_is_refused(self):
        # Better to install no route than a route for the wrong prefix.
        with self.assertRaises(ValueError):
            without_ra.prefixlen_from_netmask("ffff:0:ffff::")


class TestOnLinkPrefix(unittest.TestCase):

    def test_prefix_is_the_network_not_the_address(self):
        self.assertEqual(
            "fc00:f:2:1:1::/108",
            without_ra.on_link_prefix("fc00:f:2:1:1::16b",
                                      "ffff:ffff:ffff:ffff:ffff:ffff:fff0:0"))


class TestPlan(unittest.TestCase):

    def test_real_amphora_config_drive(self):
        self.assertEqual(
            {"fa:16:3e:9d:73:42": ["fc00:f:2:1:1::/108"]},
            without_ra.plan_from_network_data(REAL_SAMPLE))

    def test_mac_is_matched_case_insensitively(self):
        data = dict(REAL_SAMPLE)
        data["links"] = [dict(REAL_SAMPLE["links"][0],
                              ethernet_mac_address="FA:16:3E:9D:73:42")]
        self.assertIn("fa:16:3e:9d:73:42",
                      without_ra.plan_from_network_data(data))

    def test_ipv4_only_instance_gets_nothing(self):
        data = {
            "links": [{"id": "tap1", "ethernet_mac_address": "fa:16:3e:1:2:3"}],
            "networks": [{"id": "network0", "type": "ipv4_dhcp",
                          "link": "tap1"}],
        }
        self.assertEqual({}, without_ra.plan_from_network_data(data))

    def test_static_ipv6_is_left_alone(self):
        # cloud-init already writes the prefix for these; there is nothing to
        # repair, and a duplicate route would only be noise.
        data = {
            "links": [{"id": "tap1", "ethernet_mac_address": "fa:16:3e:1:2:3"}],
            "networks": [{"id": "network0", "type": "ipv6", "link": "tap1",
                          "ip_address": "fc00::5",
                          "netmask": "ffff:ffff:ffff:ffff::"}],
        }
        self.assertEqual({}, without_ra.plan_from_network_data(data))

    def test_slaac_is_left_alone(self):
        # SLAAC implies a router advertisement exists, so neither the solicit
        # nor the route is needed.
        data = {
            "links": [{"id": "tap1", "ethernet_mac_address": "fa:16:3e:1:2:3"}],
            "networks": [{"id": "network0", "type": "ipv6_slaac",
                          "link": "tap1"}],
        }
        self.assertEqual({}, without_ra.plan_from_network_data(data))

    def test_a_network_missing_its_netmask_is_skipped_not_guessed(self):
        data = {
            "links": [{"id": "tap1", "ethernet_mac_address": "fa:16:3e:1:2:3"}],
            "networks": [{"id": "network0", "type": "ipv6_dhcpv6-stateful",
                          "link": "tap1", "ip_address": "fc00::5"}],
        }
        self.assertEqual({}, without_ra.plan_from_network_data(data))

    def test_two_interfaces_each_get_their_own_prefix(self):
        # An amphora ends up with a management NIC and a VIP NIC; the
        # management prefix must not be installed on the tenant interface.
        data = {
            "links": [
                {"id": "tapA", "ethernet_mac_address": "fa:16:3e:aa:aa:aa"},
                {"id": "tapB", "ethernet_mac_address": "fa:16:3e:bb:bb:bb"},
            ],
            "networks": [
                {"id": "n0", "type": "ipv6_dhcpv6-stateful", "link": "tapA",
                 "ip_address": "fc00:f:2:1:1::16b",
                 "netmask": "ffff:ffff:ffff:ffff:ffff:ffff:fff0:0"},
                {"id": "n1", "type": "ipv6_dhcpv6-stateful", "link": "tapB",
                 "ip_address": "fd00:abcd::9",
                 "netmask": "ffff:ffff:ffff:ffff::"},
            ],
        }
        self.assertEqual(
            {"fa:16:3e:aa:aa:aa": ["fc00:f:2:1:1::/108"],
             "fa:16:3e:bb:bb:bb": ["fd00:abcd::/64"]},
            without_ra.plan_from_network_data(data))

    def test_empty_input_does_not_raise(self):
        self.assertEqual({}, without_ra.plan_from_network_data({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
