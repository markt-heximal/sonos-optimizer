# Sonos Analyzer

Analyze Sonos speakers on your local network — firmware, connectivity, stereo-pair topology, Wi-Fi vs Ethernet issues, and practical recommendations.

Uses the same diagnostic endpoints Sonos exposes locally (`/support/review`, UPnP zone topology). **No cloud account required.** Your speakers must be reachable on the same network as the machine running this tool.

## Requirements

- Python 3.10+
- No third-party dependencies (stdlib only)

## Install

```bash
cd sonos-analyzer
pip install -e .
```

Or run without installing:

```bash
python -m sonos_analyzer
```

## Usage

Discover speakers automatically (SSDP):

```bash
sonos-analyzer
```

Target specific IPs (useful if discovery is blocked):

```bash
sonos-analyzer --ips 192.168.1.165,192.168.1.89,192.168.1.193
```

Discovery sends from every network interface on the machine and then reads the full speaker list from the first speaker that answers, so speakers that ignore discovery are still included. To search from one interface only:

```bash
sonos-analyzer --interface-ip 192.168.1.20
```

Wi-Fi error rates: each speaker's Wi-Fi error counter resets whenever it is read, so a single reading covers an unknown span of time. The analyzer reads twice, 30 seconds apart, and reports errors per second. Change or skip the wait:

```bash
sonos-analyzer --sample-seconds 60   # longer, steadier sample
sonos-analyzer --sample-seconds 0    # skip; error rates are not reported
```

Write reports:

```bash
sonos-analyzer --json report.json --html report.html
```

Quiet mode (files only):

```bash
sonos-analyzer --ips 192.168.1.165 --json report.json --html report.html --quiet
```

## What it checks

- Speaker inventory (room name, IP, model, firmware)
- Firmware consistency across the household
- Stereo pair topology (left/right, or main/partner when the speakers do not report which side is which)
- Mixed Wi-Fi + Ethernet pairs (common cause of dropouts)
- Wi-Fi band per speaker (2.4 GHz vs 5 GHz) and measured Wi-Fi error rates, flagging speakers far above the household median
- Bridge RX drop ratios (informational)
- Trueplay / room calibration state hints
- Actionable recommendations (wire both, Wi-Fi-only fallback, DHCP reservations)

## Limitations

- Must run on the **same LAN** as your Sonos system
- Some networks block SSDP multicast — use `--ips` if discovery finds nothing
- Diagnostics are read-only; the tool does not change Sonos settings
- Findings are heuristic guidance, not official Sonos support output

## Example

```bash
sonos-analyzer --ips 192.168.1.165,192.168.1.89,192.168.1.193 \
  --json sonos-report.json \
  --html sonos-report.html
```

## License

MIT
