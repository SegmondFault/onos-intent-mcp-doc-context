#!/usr/bin/env python3
"""Tests for the generic topology control API."""

import unittest

from topology.control_api import ControlError, NetworkControl


class FakeNode:
    def __init__(self, name):
        self.name = name


class FakeIntf:
    def __init__(self, node):
        self.node = node
        self.up = True

    def isUp(self):
        return self.up


class FakeLink:
    def __init__(self, left, right):
        self.intf1 = FakeIntf(FakeNode(left))
        self.intf2 = FakeIntf(FakeNode(right))

    def set_up(self, state):
        self.intf1.up = state == "up"
        self.intf2.up = state == "up"


class FakeNet:
    def __init__(self, pairs):
        self.links = [FakeLink(left, right) for left, right in pairs]

    def configLinkStatus(self, left, right, state):
        for link in self.links:
            names = {link.intf1.node.name, link.intf2.node.name}
            if names == {left, right}:
                link.set_up(state)
                return

    def stop(self):
        pass


class FakeHost:
    def __init__(self, name, ip, mac):
        self.name = name
        self.ip = ip
        self.mac = mac

    def IP(self):
        return self.ip

    def MAC(self):
        return self.mac

    def cmd(self, command):
        self.last_command = command
        if "urllib.request" in command:
            return "200\nhello from simulated site\n"
        return "3 packets transmitted, 3 received, 0% packet loss"


class TopologyControlTests(unittest.TestCase):
    def test_status_ping_and_link(self):
        net = FakeNet([("h1", "s1"), ("s1", "s2")])
        hosts = {
            "h1": FakeHost("h1", "10.0.0.1", "00:00:00:00:00:01"),
            "h2": FakeHost("h2", "10.0.0.2", "00:00:00:00:00:02"),
        }
        switches = {"s1": object(), "s2": object()}
        control = NetworkControl(net, switches, hosts)

        status = control.status()
        self.assertTrue(status["ok"])
        self.assertEqual(len(status["links"]), 2)
        self.assertEqual(status["hosts"]["h1"]["switch"], "s1")

        ping = control.ping("h1", "h2", 2)
        self.assertTrue(ping["ok"])
        self.assertEqual(ping["loss_percent"], 0)

        fetched = control.http_get("h1", "10.0.0.2", 80, "/health")
        self.assertTrue(fetched["ok"])
        self.assertEqual(fetched["status"], 200)
        self.assertIn("urllib.request", hosts["h1"].last_command)

        with self.assertRaises(ControlError):
            control.http_get("h1", "example.com", 80, "/")
        with self.assertRaises(ControlError):
            control.http_get("h1", "10.0.0.2", 22, "/")
        with self.assertRaises(ControlError):
            control.http_get("h1", "10.0.0.2", 80, "/;touch-bad")

        down = control.set_link("s1", "s2", "down")
        self.assertEqual(down["status"], "down")
        self.assertEqual(control.status()["links"][1]["status"], "down")

        with self.assertRaises(ControlError):
            control.set_link("s1", "missing", "down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
