import json
import unittest

from brick_arena.sentry import Watch, classify, uuid16


class ClassifyTests(unittest.TestCase):
    def test_trackers_and_gadgets(self):
        self.assertEqual(classify(None, {0x004C: bytes([0x12, 0x19, 0x10])}, [], {}), "findmy")
        self.assertEqual(classify(None, {}, [uuid16(0xFD5A)], {}), "smarttag")
        self.assertEqual(classify(None, {}, [], {uuid16(0xFEED): b"\x02"}), "tile")
        self.assertEqual(classify(None, {}, [], {uuid16(0xFEAA): b"\x41abc"}), "googletag")
        self.assertEqual(classify(None, {}, [uuid16(0xFD5F)], {}), "meta")
        self.assertEqual(classify("Flipper Zorbo", {}, [], {}), "flipper")
        self.assertEqual(classify(None, {}, [uuid16(0x3082)], {}), "flipperish")
        self.assertEqual(classify("HC-05", {}, [], {}), "skimmer")

    def test_everyday_devices_are_ignored(self):
        self.assertIsNone(classify(None, {0x004C: bytes([0x10, 0x05])}, [], {}))  # an iPhone's nearby info
        self.assertIsNone(classify("JBL Flip 5", {0x0057: b"\x01"}, [], {}))
        self.assertIsNone(classify(None, {}, [], {uuid16(0xFEAA): b"\x10url"}))  # a plain Eddystone URL beacon
        self.assertIsNone(classify(None, {0x004C: b""}, [], {}))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.w = Watch(learn_seconds=600, linger_seconds=120, gone_seconds=120, started=0)

    def heard(self, t, *addresses, kind="findmy", rssi=-60):
        for a in addresses:
            self.w.advert(a, kind, rssi, now=t)
        self.w.tick(now=t)

    def test_the_house_normal_is_learned_and_never_alerts(self):
        for t in range(0, 1200, 10):
            self.heard(t, "AA", "BB")
        self.assertEqual(self.w.normal, {"findmy": 2})
        self.assertEqual(self.w.alerts, [])

    def test_a_newcomer_lingering_past_the_normal_alerts_once(self):
        for t in range(0, 600, 10):
            self.heard(t, "AA")
        for t in range(600, 720, 10):
            self.heard(t, "AA", "CC")
        self.assertEqual(self.w.alerts, [])  # not yet two minutes over
        for t in range(720, 900, 10):
            self.heard(t, "AA", "CC")
        self.assertEqual(len(self.w.alerts), 1)
        a = self.w.alerts[0]
        self.assertEqual((a["kind"], a["count"], a["normal"]), ("findmy", 2, 1))

    def test_a_passer_by_does_not_alert(self):
        for t in range(0, 600, 10):
            self.heard(t, "AA")
        for t in range(600, 660, 10):
            self.heard(t, "AA", "CC")
        for t in range(660, 1000, 10):
            self.heard(t, "AA")
        self.assertEqual(self.w.alerts, [])

    def test_low_confidence_kinds_show_but_never_alert(self):
        for t in range(600, 1200, 10):
            self.heard(t, "DD", kind="flipperish")
        self.assertEqual(self.w.alerts, [])
        self.assertEqual(self.w.get(now=1200)["kinds"][0]["count"], 1)

    def test_devices_drop_off_after_gone_seconds(self):
        self.heard(0, "AA")
        self.assertEqual(len(self.w.get(now=100)["blips"]), 1)
        self.assertEqual(self.w.get(now=121)["blips"], [])

    def test_no_address_reaches_the_state(self):
        self.heard(0, "F0:12:34:56:78:9A", rssi=-70)
        state = json.dumps(self.w.get(now=1))
        self.assertNotIn("F0:12", state)
        self.assertNotIn("56:78", state)
        # And the ids differ between starts (a new salt each time).
        other = Watch(started=0)
        other.advert("F0:12:34:56:78:9A", "findmy", -70, now=0)
        self.assertNotEqual(other.get(now=1)["blips"][0]["id"], self.w.get(now=1)["blips"][0]["id"])
