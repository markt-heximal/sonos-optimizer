"""Command-line interface for sonos-analyzer."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sonos_analyzer.analyze import analyze
from sonos_analyzer.discovery import discover_speakers
from sonos_analyzer.fetch import (
    fetch_device_description,
    fetch_review,
    fetch_zone_group_state,
)
from sonos_analyzer.parse import (
    HouseholdSnapshot,
    merge_speakers,
    parse_device_description,
    parse_review_xml,
    parse_zone_group_state,
)
from sonos_analyzer.report import print_terminal, write_html, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze Sonos speakers on your local network.",
        epilog="""Examples:
  sonos-analyzer
  sonos-analyzer --ips 192.168.1.165,192.168.1.89
  sonos-analyzer --json report.json --html report.html
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ips",
        help="Comma-separated speaker IPs (skip SSDP discovery)",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        help="SSDP discovery timeout in seconds (default: 3)",
    )
    parser.add_argument(
        "--interface-ip",
        action="append",
        metavar="ADDR",
        help="Local IPv4 address to send discovery from (repeatable; default: all interfaces)",
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=30.0,
        help="Seconds between two diagnostic reads used to measure Wi-Fi error rates "
        "(default: 30; 0 skips the measurement)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Write JSON report to PATH",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="Write HTML report to PATH",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress terminal report (useful with --json/--html only)",
    )

    args = parser.parse_args(argv)

    if args.ips:
        ips = [ip.strip() for ip in args.ips.split(",") if ip.strip()]
    else:
        discovered = discover_speakers(
            timeout=args.discover_timeout,
            interface_ips=args.interface_ip,
        )
        ips = [d.ip for d in discovered]
        if not ips:
            print(
                "No Sonos speakers discovered via SSDP. "
                "Try --ips 192.168.1.x or open a speaker's IP in a browser to confirm it is reachable.",
                file=sys.stderr,
            )
            return 1

    snapshot = _collect_snapshot(
        ips,
        sample_seconds=max(0.0, args.sample_seconds),
        progress=not args.quiet,
    )
    if not snapshot.speakers:
        print("Could not read diagnostics from any speaker.", file=sys.stderr)
        for err in snapshot.fetch_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    report = analyze(snapshot)

    if not args.quiet:
        print_terminal(report)

    if args.json:
        write_json(report, Path(args.json))
        if not args.quiet:
            print(f"Wrote JSON report to {args.json}")

    if args.html:
        write_html(report, Path(args.html))
        if not args.quiet:
            print(f"Wrote HTML report to {args.html}")

    return 0


def _collect_snapshot(
    ips: list[str],
    sample_seconds: float = 0.0,
    progress: bool = False,
) -> HouseholdSnapshot:
    snapshot = HouseholdSnapshot(source_ips=list(ips))
    group_maps: list[dict] = []
    descriptions: dict[str, dict[str, str]] = {}
    unreachable: set[str] = set()

    # 1. Topology: one reachable speaker lists every household member, which
    #    also catches speakers that did not answer discovery.
    queue = list(ips)
    for ip in list(queue):
        try:
            group_map = parse_zone_group_state(fetch_zone_group_state(ip))
        except Exception as exc:  # noqa: BLE001 - try the next speaker
            snapshot.fetch_errors.append(f"{ip} zone group: {exc}")
            continue
        group_maps.append(group_map)
        for member_ip in group_map:
            if member_ip not in queue:
                queue.append(member_ip)
        break

    # 2. Device descriptions (model name) for every speaker.
    for ip in queue:
        try:
            descriptions[ip] = parse_device_description(fetch_device_description(ip))
        except Exception as exc:  # noqa: BLE001 - surface as non-fatal
            snapshot.fetch_errors.append(f"{ip}: {exc}")
            unreachable.add(ip)

    # 3. Diagnostics. /support/review on one speaker returns every speaker, and
    #    reading it resets each Wi-Fi PHY error counter. Read once, wait a known
    #    time, read again: the second reading is errors over that window.
    review_map: dict = {}
    review_ip = None
    first_read_at = 0.0
    for ip in queue:
        if ip in unreachable:
            continue
        try:
            started = time.monotonic()
            review_map = parse_review_xml(fetch_review(ip))
            first_read_at = (started + time.monotonic()) / 2
            review_ip = ip
            break
        except Exception as exc:  # noqa: BLE001 - try the next speaker
            snapshot.fetch_errors.append(f"{ip} review: {exc}")

    if review_ip and sample_seconds > 0:
        if progress:
            print(
                f"Sampling Wi-Fi error rates for {sample_seconds:g}s "
                "(use --sample-seconds 0 to skip)...",
                file=sys.stderr,
            )
        time.sleep(sample_seconds)
        try:
            started = time.monotonic()
            second = parse_review_xml(fetch_review(review_ip))
            elapsed = (started + time.monotonic()) / 2 - first_read_at
            for sp in second.values():
                if sp.phy_errors is not None and elapsed > 0:
                    sp.phy_errors_per_sec = sp.phy_errors / elapsed
            review_map = second
            snapshot.sample_seconds = elapsed
        except Exception as exc:  # noqa: BLE001 - keep the first reading
            snapshot.fetch_errors.append(f"{review_ip} review (second sample): {exc}")

    # Speakers missing from the household review get their own fetch.
    extra_reviews = []
    for ip in queue:
        if ip in review_map or ip in unreachable or review_ip is None:
            continue
        try:
            extra_reviews.append(parse_review_xml(fetch_review(ip)))
        except Exception as exc:  # noqa: BLE001
            snapshot.fetch_errors.append(f"{ip} review: {exc}")

    snapshot.speakers = merge_speakers(review_map, *extra_reviews, *group_maps)
    snapshot.source_ips = queue

    for ip, meta in descriptions.items():
        if ip not in snapshot.speakers:
            continue
        speaker = snapshot.speakers[ip]
        speaker.model_name = meta.get("model_name") or speaker.model_name
        speaker.model_number = meta.get("model_number") or speaker.model_number
        speaker.display_version = meta.get("display_version") or speaker.display_version

    return snapshot


if __name__ == "__main__":
    raise SystemExit(main())
