"""Analyze parsed Sonos diagnostics and produce findings."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics, parse_channel_map

PHY_RATE_FLOOR = 50.0  # errors per second
PHY_RATE_MEDIAN_MULTIPLE = 3.0


@dataclass
class Finding:
    severity: str  # high, medium, low, ok
    title: str
    detail: str
    affected_ips: list[str] = field(default_factory=list)


@dataclass
class AnalysisReport:
    generated_at: str
    speaker_count: int
    firmware_versions: dict[str, list[str]]
    speakers: list[dict]
    findings: list[Finding]
    recommendations: list[str]
    snapshot: HouseholdSnapshot


def analyze(snapshot: HouseholdSnapshot) -> AnalysisReport:
    speakers = list(snapshot.speakers.values())
    findings: list[Finding] = []
    recommendations: list[str] = []

    firmware_versions: dict[str, list[str]] = {}
    for sp in speakers:
        firmware_versions.setdefault(sp.software_version or "unknown", []).append(sp.ip_address)

    if len(firmware_versions) > 1:
        versions = ", ".join(sorted(firmware_versions))
        findings.append(
            Finding(
                severity="medium",
                title="Firmware versions differ across speakers",
                detail=f"Found multiple versions: {versions}. All speakers should run the same version.",
                affected_ips=[ip for ips in firmware_versions.values() for ip in ips],
            )
        )
    elif speakers:
        version = next(iter(firmware_versions))
        findings.append(
            Finding(
                severity="ok",
                title="Firmware matched across household",
                detail=f"All speakers report {version}.",
                affected_ips=[sp.ip_address for sp in speakers],
            )
        )

    for first, second in _stereo_pairs(speakers):
        first_eth = _is_ethernet(first)
        second_eth = _is_ethernet(second)
        pair_name = _base_zone_name(first.zone_name)
        members = [first.ip_address, second.ip_address]

        if first_eth != second_eth:
            wired, wireless = (first, second) if first_eth else (second, first)
            findings.append(
                Finding(
                    severity="high",
                    title=f"Stereo pair '{pair_name}' is mixed Wi-Fi + Ethernet",
                    detail=(
                        f"{_role_label(wired)} speaker ({wired.ip_address}) is wired, while "
                        f"{_role_label(wireless).lower()} speaker ({wireless.ip_address}) is on Wi-Fi. "
                        "Sonos stereo pairs work best when both speakers share the same network path: "
                        "wire both, or unplug the cable and run both on Wi-Fi."
                    ),
                    affected_ips=members,
                )
            )
            recommendations.append(
                f"{pair_name} stereo pair: wire both speakers, or unplug Ethernet and run both on Wi-Fi."
            )
        elif first_eth and second_eth:
            findings.append(
                Finding(
                    severity="ok",
                    title=f"Stereo pair '{pair_name}' is fully wired",
                    detail="Both speakers use Ethernet — ideal topology for a stereo pair.",
                    affected_ips=members,
                )
            )

    wireless = [sp for sp in speakers if not _is_ethernet(sp) and _channel_mhz(sp) is not None]
    on_24 = [sp for sp in wireless if _channel_mhz(sp) < 3000]
    if on_24:
        rooms = ", ".join(sorted({_base_zone_name(sp.zone_name) for sp in on_24}))
        findings.append(
            Finding(
                severity="low",
                title=f"{len(on_24)} of {len(wireless)} wireless speakers use 2.4 GHz Wi-Fi",
                detail=(
                    f"Rooms: {rooms}. 2.4 GHz is usually more crowded than 5 GHz and is where "
                    "most interference shows up. Ethernet avoids Wi-Fi congestion entirely "
                    "if a cable is available."
                ),
                affected_ips=[sp.ip_address for sp in on_24],
            )
        )

    phy_threshold = _phy_rate_threshold(speakers)

    for sp in speakers:
        drop_ratio = _rx_drop_ratio(sp)
        if drop_ratio is not None and drop_ratio >= 20:
            findings.append(
                Finding(
                    severity="medium",
                    title=f"High bridge RX drop ratio on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} ({sp.ip_address}) shows {drop_ratio:.1f}% br0 RX drops "
                        f"({sp.br0.rx_dropped:,} / {sp.br0.rx_packets:,}). "
                        "Sonos counts filtered multicast here, but unusually high ratios can indicate stress."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        rate = sp.phy_errors_per_sec
        if phy_threshold is not None and rate is not None and rate >= phy_threshold:
            band = "2.4 GHz" if (_channel_mhz(sp) or 0) < 3000 else "5 GHz"
            findings.append(
                Finding(
                    severity="medium",
                    title=f"High Wi-Fi error rate on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} logged {rate:,.0f} Wi-Fi PHY errors per second over "
                        f"{snapshot.sample_seconds:.0f}s on {band} (household typical: "
                        f"{_median_phy_rate(speakers):,.0f}/s). Errors this far above the rest of "
                        "the house usually mean weak signal or interference near this speaker."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        if sp.room_calibration_state == "1":
            findings.append(
                Finding(
                    severity="low",
                    title=f"Trueplay may be incomplete on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} reports room calibration state 1. "
                        "Re-run Trueplay from the Sonos app after changing speaker placement or wiring."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        if sp.br0.rx_errors or sp.br0.tx_errors or sp.eth0.rx_errors or sp.eth0.tx_errors:
            findings.append(
                Finding(
                    severity="high",
                    title=f"Interface errors on {sp.zone_name}",
                    detail=f"{sp.zone_name} reports non-zero interface errors in ifconfig output.",
                    affected_ips=[sp.ip_address],
                )
            )

    if snapshot.sample_seconds <= 0:
        findings.append(
            Finding(
                severity="low",
                title="Wi-Fi error rates not measured",
                detail=(
                    "Each speaker's Wi-Fi error counter resets whenever it is read, so a single "
                    "reading covers an unknown span of time. Run with --sample-seconds 30 (the "
                    "default) to measure errors per second."
                ),
            )
        )

    if not recommendations:
        recommendations.append(
            "No critical topology issues detected. Consider fixed DHCP reservations for speaker IPs."
        )

    recommendations.extend(
        [
            "Reserve DHCP addresses for each speaker so IPs stay stable.",
            "Keep router 2.4 GHz on 20 MHz channel width for Sonos compatibility.",
            "Re-run Trueplay after any wiring or placement changes.",
        ]
    )
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_recs: list[str] = []
    for rec in recommendations:
        if rec not in seen:
            seen.add(rec)
            unique_recs.append(rec)

    speaker_rows = [_speaker_to_dict(sp) for sp in sorted(speakers, key=lambda s: s.zone_name)]

    return AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        speaker_count=len(speakers),
        firmware_versions=firmware_versions,
        speakers=speaker_rows,
        findings=sorted(findings, key=_severity_rank),
        recommendations=unique_recs,
        snapshot=snapshot,
    )


def _stereo_pairs(speakers: list[SpeakerDiagnostics]) -> list[tuple[SpeakerDiagnostics, SpeakerDiagnostics]]:
    """Return (first, second) for every two-speaker stereo pair.

    Order is left/main first when known. Works whether or not the channel map
    tells the sides apart.
    """
    by_uid = {sp.local_uid: sp for sp in speakers if sp.local_uid}
    pairs: list[tuple[SpeakerDiagnostics, SpeakerDiagnostics]] = []
    seen: set[tuple[str, ...]] = set()
    order = {"left": 0, "main": 0, "right": 1, "partner": 1}

    for sp in speakers:
        uids = list(parse_channel_map(sp.channel_map_set))
        if len(uids) != 2:
            continue
        members = [by_uid.get(uid) for uid in uids]
        if any(m is None for m in members):
            continue
        key = tuple(sorted(m.ip_address for m in members))
        if key in seen:
            continue
        seen.add(key)
        members.sort(key=lambda m: order.get(m.stereo_role, 2))
        pairs.append((members[0], members[1]))

    return pairs


def _role_label(sp: SpeakerDiagnostics) -> str:
    return {
        "left": "The left",
        "right": "The right",
        "main": "The main",
        "partner": "The partner",
    }.get(sp.stereo_role, "One")


def _base_zone_name(name: str) -> str:
    # Sonos appends channel roles, e.g. "Living Room (L+R)" or "Den (LF,RF)".
    return re.sub(r"\s*\([A-Z+,]+\)\s*$", "", name or "").strip() or name


def _channel_mhz(sp: SpeakerDiagnostics) -> int | None:
    try:
        return int(sp.wifi_channel_mhz)
    except (TypeError, ValueError):
        return None


def _median_phy_rate(speakers: list[SpeakerDiagnostics]) -> float:
    rates = [sp.phy_errors_per_sec for sp in speakers if sp.phy_errors_per_sec is not None]
    return statistics.median(rates) if rates else 0.0


def _phy_rate_threshold(speakers: list[SpeakerDiagnostics]) -> float | None:
    """Flag speakers well above the rest of the household.

    There is no published "normal" PHY error rate, so compare speakers to each
    other: at least 3x the household median and at least 50 errors/second.
    """
    rates = [sp.phy_errors_per_sec for sp in speakers if sp.phy_errors_per_sec is not None]
    if not rates:
        return None
    return max(PHY_RATE_FLOOR, PHY_RATE_MEDIAN_MULTIPLE * statistics.median(rates))


def _is_ethernet(sp: SpeakerDiagnostics) -> bool:
    conn = (sp.connection_type or "").lower()
    return "ethernet" in conn or sp.ethernet_link or sp.connection_type_code == "4"


def _rx_drop_ratio(sp: SpeakerDiagnostics) -> float | None:
    if sp.br0.rx_packets <= 0:
        return None
    return 100.0 * sp.br0.rx_dropped / sp.br0.rx_packets


def _severity_rank(finding: Finding) -> int:
    return {"high": 0, "medium": 1, "low": 2, "ok": 3}.get(finding.severity, 4)


def _speaker_to_dict(sp: SpeakerDiagnostics) -> dict:
    drop_ratio = _rx_drop_ratio(sp)
    return {
        "zone_name": sp.zone_name,
        "ip_address": sp.ip_address,
        "model_name": sp.model_name,
        "model_number": sp.model_number,
        "software_version": sp.software_version,
        "display_version": sp.display_version,
        "connection_type": sp.connection_type,
        "ethernet_link": sp.ethernet_link,
        "ethernet_speed": sp.ethernet_speed,
        "wifi_channel_mhz": sp.wifi_channel_mhz,
        "wifi_mode": sp.wifi_mode,
        "room_calibration_state": sp.room_calibration_state,
        "boot_seq": sp.boot_seq,
        "airplay_enabled": sp.airplay_enabled,
        "is_stereo_left": sp.is_stereo_left,
        "is_stereo_right": sp.is_stereo_right,
        "is_stereo_coordinator": sp.is_stereo_coordinator,
        "stereo_role": sp.stereo_role,
        "br0_rx_drop_ratio_pct": round(drop_ratio, 1) if drop_ratio is not None else None,
        "br0_rx_packets": sp.br0.rx_packets,
        "br0_rx_dropped": sp.br0.rx_dropped,
        "phy_errors": sp.phy_errors,
        "phy_errors_per_sec": round(sp.phy_errors_per_sec, 1) if sp.phy_errors_per_sec is not None else None,
    }
