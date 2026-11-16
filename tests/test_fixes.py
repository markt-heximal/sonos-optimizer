"""Regression tests for parsing and analysis fixes (synthetic data only)."""

from __future__ import annotations

import html
import unittest

from sonos_analyzer.analyze import _phy_rate_threshold, _stereo_pairs, analyze
from sonos_analyzer.parse import (
    HouseholdSnapshot,
    SpeakerDiagnostics,
    merge_speakers,
    parse_device_description,
    parse_zone_group_state,
)

DEVICE_XML = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <modelName>Sonos Example 100</modelName>
    <modelNumber>S99</modelNumber>
    <displayVersion>1.2</displayVersion>
    <roomName>Test Room</roomName>
  </device>
</root>"""


def _member(uuid: str, ip: str, zone: str, chan_map: str = "", invisible: bool = False,
            freq: str = "5180") -> str:
    attrs = (
        f'UUID="{uuid}" Location="http://{ip}:1400/xml/device_description.xml" '
        f'ZoneName="{zone}" ConnectionType="5" ChannelFreq="{freq}"'
    )
    if chan_map:
        attrs += f' ChannelMapSet="{chan_map}"'
    if invisible:
        attrs += ' Invisible="1"'
    return f"<ZoneGroupMember {attrs}/>"


def _zgs(*members: str, coordinator: str) -> str:
    inner = "".join(members)
    xml = (
        f'<ZoneGroupState><ZoneGroups><ZoneGroup Coordinator="{coordinator}" ID="{coordinator}:1">'
        f"{inner}</ZoneGroup></ZoneGroups></ZoneGroupState>"
    )
    return html.escape(xml)


class DeviceDescriptionTests(unittest.TestCase):
    def test_model_name_is_read(self) -> None:
        meta = parse_device_description(DEVICE_XML)
        self.assertEqual(meta["model_name"], "Sonos Example 100")
        self.assertEqual(meta["model_number"], "S99")
        self.assertEqual(meta["display_version"], "1.2")


class StereoRoleTests(unittest.TestCase):
    def test_distinct_sides_are_left_and_right(self) -> None:
        cmap = "RINCON_A:LF,LF;RINCON_B:RF,RF"
        speakers = parse_zone_group_state(_zgs(
            _member("RINCON_A", "10.9.0.1", "Den", cmap),
            _member("RINCON_B", "10.9.0.2", "Den", cmap, invisible=True),
            coordinator="RINCON_A",
        ))
        self.assertEqual(speakers["10.9.0.1"].stereo_role, "left")
        self.assertTrue(speakers["10.9.0.1"].is_stereo_left)
        self.assertEqual(speakers["10.9.0.2"].stereo_role, "right")
        self.assertTrue(speakers["10.9.0.2"].is_stereo_right)

    def test_identical_roles_are_main_and_partner_not_both_right(self) -> None:
        cmap = "RINCON_A:LF,RF;RINCON_B:LF,RF"
        speakers = parse_zone_group_state(_zgs(
            _member("RINCON_A", "10.9.0.1", "Den", cmap),
            _member("RINCON_B", "10.9.0.2", "Den", cmap, invisible=True),
            coordinator="RINCON_A",
        ))
        main, partner = speakers["10.9.0.1"], speakers["10.9.0.2"]
        self.assertEqual(main.stereo_role, "main")
        self.assertEqual(partner.stereo_role, "partner")
        for sp in (main, partner):
            self.assertFalse(sp.is_stereo_left)
            self.assertFalse(sp.is_stereo_right)

    def test_pairs_found_even_when_sides_unknown(self) -> None:
        cmap = "RINCON_A:LF,RF;RINCON_B:LF,RF"
        speakers = parse_zone_group_state(_zgs(
            _member("RINCON_A", "10.9.0.1", "Den", cmap),
            _member("RINCON_B", "10.9.0.2", "Den", cmap, invisible=True),
            coordinator="RINCON_A",
        ))
        pairs = _stereo_pairs(list(speakers.values()))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0].stereo_role, "main")


class WifiBandTests(unittest.TestCase):
    def _snapshot(self, freqs: list[str]) -> HouseholdSnapshot:
        members = [
            _member(f"RINCON_{i}", f"10.9.0.{i + 1}", f"Room {i}", freq=f)
            for i, f in enumerate(freqs)
        ]
        speakers = parse_zone_group_state(_zgs(*members, coordinator="RINCON_0"))
        return HouseholdSnapshot(speakers=merge_speakers(speakers), sample_seconds=30)

    def test_only_24ghz_speakers_flagged(self) -> None:
        report = analyze(self._snapshot(["2412", "5180", "5745", "2462"]))
        band = [f for f in report.findings if "2.4 GHz" in f.title]
        self.assertEqual(len(band), 1)
        self.assertTrue(band[0].title.startswith("2 of 4"))
        self.assertEqual(sorted(band[0].affected_ips), ["10.9.0.1", "10.9.0.4"])

    def test_no_24ghz_finding_when_all_5ghz(self) -> None:
        report = analyze(self._snapshot(["5180", "5745"]))
        self.assertFalse([f for f in report.findings if "2.4 GHz" in f.title])


class PhyRateTests(unittest.TestCase):
    def _speakers(self, rates: list[float]) -> list[SpeakerDiagnostics]:
        out = []
        for i, rate in enumerate(rates):
            sp = SpeakerDiagnostics(zone_name=f"Room {i}", ip_address=f"10.9.0.{i + 1}")
            sp.phy_errors_per_sec = rate
            out.append(sp)
        return out

    def test_only_outliers_flagged(self) -> None:
        speakers = self._speakers([2, 3, 20, 30, 30, 40, 400, 700])
        snapshot = HouseholdSnapshot(
            speakers={sp.ip_address: sp for sp in speakers}, sample_seconds=30
        )
        report = analyze(snapshot)
        flagged = sorted(
            ip for f in report.findings if "Wi-Fi error rate" in f.title for ip in f.affected_ips
        )
        self.assertEqual(flagged, ["10.9.0.7", "10.9.0.8"])

    def test_floor_prevents_flagging_a_quiet_house(self) -> None:
        # Median 1/s -> 3x would be 3/s; the 50/s floor keeps 10/s unflagged.
        threshold = _phy_rate_threshold(self._speakers([1, 1, 1, 10]))
        self.assertEqual(threshold, 50.0)

    def test_unmeasured_rates_are_reported_not_guessed(self) -> None:
        sp = SpeakerDiagnostics(zone_name="Room", ip_address="10.9.0.1", phy_errors=9_000_000)
        report = analyze(HouseholdSnapshot(speakers={sp.ip_address: sp}, sample_seconds=0))
        titles = [f.title for f in report.findings]
        self.assertIn("Wi-Fi error rates not measured", titles)
        self.assertFalse([t for t in titles if "High Wi-Fi error rate" in t])


if __name__ == "__main__":
    unittest.main()
