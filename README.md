# PolyTool

Open-source toolkit for Poly/Plantronics USB headsets. Replaces the official Poly Lens desktop app with a lightweight web dashboard, CLI tools, and a fleet management server.

## Features

- **Web Dashboard** — Device management, firmware updates, and settings at `localhost:8420`
- **Firmware Updates** — Download from Poly Cloud and flash directly over USB HID
- **Device Settings** — Sidetone, EQ, volume, mute, and more
- **Fleet Management** — Central server with PostgreSQL, agent-based reporting, policy enforcement
- **Remote Configuration** — Push settings changes to headsets across your network
- **Identity Preservation** — Firmware flashing preserves device serial numbers and Poly Lens compatibility
- **28+ Devices Supported** — Blackwire, Savi, Sync, Voyager, EncorePro series

## Quick Start

**Single workstation:**
```bash
pip install -r requirements.txt
python3 polylens.py
```

**Fleet management:**
```bash
# Server (requires PostgreSQL)
python3 polyserver.py --db postgresql://user:pass@localhost/polytool

# Agent (on each workstation)
python3 polyagent.py --server http://server:8421 --key <agent_key>
```

## Supported Devices

| Device | Flash | Settings | Protocol |
|--------|-------|----------|----------|
| Blackwire 3220 | Yes | Sidetone | CX2070x EEPROM |
| Blackwire 3310/3315/3320/3325 | Yes | Full | BladeRunner HID |
| Blackwire 7225/8225 | Yes | Full | BladeRunner HID |
| Savi 7310/7320/7410/7420 | Yes | Via base | FWU API |
| Savi 8200/8210/8220/8410/8420 | Yes | Via base | FWU API |
| Sync 20 | Yes | — | BladeRunner HID |
| EncorePro 310/320/515/525/545 | Yes | Full | BladeRunner HID |
| Voyager Legend | Untested | — | USB DFU |

## Tools

| File | Description |
|------|-------------|
| `polylens.py` | Web dashboard for single workstation |
| `polyserver.py` | Fleet management server (PostgreSQL) |
| `polyagent.py` | Workstation agent for fleet reporting |
| `polyremote.py` | CLI for remote settings changes |
| `polytool.py` | CLI for scan, info, battery, updates, flash |
| `menu.py` | Interactive terminal menu |
| `fwu_flash.py` | Savi DECT firmware flasher |
| `bw_flash.py` | Blackwire 3220 EEPROM flasher |
| `device_identity.py` | Device identity preservation during flash |
| `device_settings.py` | HID settings read/write per device family |
| `probes/` | HID protocol research tools |

## Web Dashboard

```bash
python3 polylens.py
```

- **Devices** — Connected headsets with firmware, battery, serial. Auto-refreshes.
- **Updates** — Check Poly Cloud, download, and flash with one click.
- **Settings** — Sidetone, EQ, noise limiting, wearing sensor, language, and more.
- **Catalog** — Search all Poly firmware online.

## Fleet Management

Central server for managing headsets across an organization.

**Server** (`polyserver.py`):
```bash
# Requires PostgreSQL
python3 polyserver.py --db postgresql://user:pass@localhost/polytool
```

The server prints API keys on startup. Use the agent key to connect workstations.

**Agent** (`polyagent.py`):
```bash
# Run on each workstation — reports devices every 60s
python3 polyagent.py --server http://server:8421 --key <agent_key>

# Single report and exit
python3 polyagent.py --server http://server:8421 --key <agent_key> --once
```

**Admin Dashboard** at `http://server:8421`:
- **Overview** — Device counts, firmware versions, online agents, recent activity
- **Devices** — Every headset across the org with online/offline status, host, user
- **Compliance** — Policy violations with details per device
- **Policies** — Enforce firmware versions and settings across the fleet
- **Commands** — Push settings changes to all agents
- **Audit Log** — Full activity history

**Security:**
- API key authentication for agents and admin dashboard
- Rate limiting (120 req/min per IP)
- Input validation on all endpoints
- Keys auto-generated on first run, stored with restricted permissions

## PolyRemote

Standalone CLI for headset settings. Coexists with Poly Lens — does not modify firmware or the managed client.

```bash
python3 polyremote.py list                              # List devices
python3 polyremote.py set "Sidetone Level" 5            # Change a setting
python3 polyremote.py set "Ringtone Volume" 8 --pid 0xACFF  # Target specific device
python3 polyremote.py get "Sidetone Level"              # Read a setting
python3 polyremote.py dump                              # Dump all settings
python3 polyremote.py batch presets/office_standard.json # Apply preset
python3 polyremote.py settings                          # List available settings
```

JSON output for automation (auto-enabled when piped):
```bash
python3 polyremote.py list --json | jq '.devices[].name'
python3 polyremote.py dump --json > device_report.json
```

Included presets in `presets/`:
- `office_standard.json` — Standard office config
- `call_center.json` — High-volume call center
- `quiet_mode.json` — Minimal audio feedback

## CLI Usage

```bash
python3 polytool.py scan       # Discover devices
python3 polytool.py info       # Detailed device info
python3 polytool.py battery    # Battery status
python3 polytool.py updates    # Check for firmware updates
python3 polytool.py update     # Download and flash firmware
python3 polytool.py catalog    # Search Poly firmware catalog
python3 menu.py                # Interactive terminal menu
```

## Firmware Flashing

Three protocols, all reverse-engineered from Poly Lens:

**CX2070x EEPROM** (Blackwire 3220) — S-record patches to Conexant EEPROM via HID. Automatically preserves serial number and calibration data.

**FWU API** (Savi series) — CVM mailbox protocol with LE 16-bit primitive IDs. Tested: 4,915 blocks, 991 KB, 89 seconds.

**BladeRunner FTP** (Blackwire 33xx, Sync, EncorePro) — File transfer over HID with handshake, block writes, and CRC32 verification.

## Requirements

- Python 3.9+
- `hidapi` — HID device communication
- `requests` — Poly Cloud API
- `flask` — Web dashboard and server
- `pyusb` — USB reset for Savi flash (macOS)
- `psycopg2-binary` — PostgreSQL (fleet server)
- `rich` — Terminal formatting (optional)

```bash
pip install -r requirements.txt
```

## How It Works

PolyTool talks directly to headsets over USB HID using the same protocols Poly Lens uses internally. Firmware is downloaded from Poly's public CDN and flashed using reverse-engineered protocols from `libDFUManager.dylib`.

See `RE_FINDINGS.txt` for the complete reverse engineering documentation — register maps, CVM commands, EEPROM layouts, and protocol details for every tested device.

## License

MIT
