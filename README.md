# PolyTool

Open-source toolkit for managing Poly/Plantronics USB headsets. Drop-in replacement for the official Poly Lens desktop app — includes a web dashboard, CLI tools, a drop-in LensService replacement that serves devices directly to the Poly Studio GUI with full settings control, firmware flashing, and enterprise fleet management.

## Features

- **LensServer** — Drop-in Poly Lens Control Service replacement. Poly Studio GUI connects to it and displays your devices with full settings controls, product images, battery status, and firmware info — no official Poly software required.
- **Native Bridge** — Direct ctypes interface to Poly's native libraries. Discovers USB and Bluetooth devices, reads/writes all settings on any Poly headset including DECT and Voyager series. No legacyhost or Clockwork needed.
- **Dynamic Settings** — Automatically detects what settings each headset supports and builds a matching profile. 72 known setting IDs across all device families with canonical per-device profiles for 217 devices loaded from Poly's own DeviceSettings.zip. Plug in any Poly headset and its settings just work.
- **Web Dashboard** — Lightweight Flask app for device management, firmware updates, and settings at `localhost:8420`
- **Firmware Flashing** — Download from Poly Cloud CDN and flash directly over USB HID with identity preservation
- **Fleet Management** — Central PostgreSQL server with agent-based reporting, policy enforcement, and compliance monitoring
- **Remote Configuration** — Push settings changes to headsets across your network
- **Interactive Menu** — Terminal UI for all device operations, firmware tools, and protocol debugging
- **Protocol Research** — HID probes, DECT settings write protocol discovery, and reverse-engineered documentation

## Quick Start

```bash
pip install hidapi requests flask

# Start the server — Poly Studio connects automatically
python3 lensserver.py

# Or use verbose mode for debugging
python3 lensserver.py --verbose
```

## Supported Devices

| Device | Flash | Settings | Protocol |
|--------|-------|----------|----------|
| Blackwire 3220 | Yes | Dialtone | CX2070x EEPROM |
| Blackwire 5xx | Yes | Dialtone | CX2070x EEPROM |
| Blackwire 3310/3315/3320/3325 | Yes | Full (17 settings) | BladeRunner HID |
| Blackwire 7225/8225 | Yes | Full (17 settings) | BladeRunner HID |
| Savi 7310/7320/7410/7420 | Yes | Full (38 settings) | Native Bridge / DECT |
| Savi 8200/8210/8220/8410/8420 | Yes | Full (38 settings) | Native Bridge / DECT |
| Voyager 4320/Focus 2/Free 60 | Untested | Full (36 settings) | Native Bridge / BT |
| Voyager Base-M CD | — | Full (6 settings) | Native Bridge / BT |
| Sync 20/40/60 | Yes | Full (17 settings) | BladeRunner HID |
| EncorePro 310/320/515/525/545 | Yes | Full (17 settings) | BladeRunner HID |
| Any other Poly headset | — | Auto-detected (217 PIDs) | Native Bridge |

Settings for any Poly headset are auto-detected via the native bridge — no manual profiles needed.

## Tools

| File | Description |
|------|-------------|
| `lensserver.py` | Drop-in Poly Lens Control Service replacement (TCP server for Poly Studio GUI) |
| `native_bridge.py` | Direct ctypes interface to Poly's native dylibs for DECT/BT settings |
| `lensapi.py` | TCP client for LensServiceApi — query devices, read/write settings, monitor events |
| `polylens.py` | Web dashboard (Flask) for single-workstation device management |
| `polytool.py` | CLI for scan, info, battery, updates, flash, catalog |
| `menu.py` | Interactive terminal menu with device, firmware, and debug submenus |
| `lens_settings.py` | Settings profiles and API format conversion |
| `device_settings_db.py` | Canonical settings database from DeviceSettings.zip (217 devices, 72 settings) |
| `device_settings.py` | Direct HID settings read/write with Poly Studio name translation |
| `polyserver.py` | Fleet management server (PostgreSQL) |
| `polyagent.py` | Workstation agent for fleet reporting |
| `polyremote.py` | CLI for remote settings changes with JSON presets |
| `fwu_flash.py` | Savi DECT firmware flasher (FWU API / CVM mailbox) |
| `bw_flash.py` | Blackwire 3220 EEPROM flasher (CX2070x S-record) |
| `device_identity.py` | Device identity preservation during flash |
| `clockwork_client.py` | Backwards-compatible wrapper around lensapi.py |
| `polybus.py` | Native PolyBus library interface (ctypes) |
| `monitor_legacyhost.py` | Poly Lens log file monitor with pattern highlighting |
| `probes/` | HID protocol research and testing tools |

## LensServer — Poly Studio Integration

Replace the official Poly Lens Control Service with our open-source implementation. Poly Studio GUI auto-connects and displays your devices with full settings controls.

```bash
python3 lensserver.py              # Auto-assigns port, writes port file
python3 lensserver.py --port 9001  # Fixed port
python3 lensserver.py --dump       # Log all messages to dump.jsonl
```

**How it works:**
1. Scans for USB devices via HID and Bluetooth devices via native bridge
2. Starts a TCP server speaking the LensServiceApi protocol (SOH-delimited JSON)
3. Writes the port number to `~/Library/Application Support/Poly/Lens Control Service/SocketPortNumber`
4. Poly Studio GUI connects automatically
5. Serves device info, product images (from Poly Cloud), settings metadata, setting values, and battery status
6. Settings writes go through direct HID (CX2070x/BladeRunner) or native bridge (DECT/Voyager/BT)
7. Restores the original LCS port file on shutdown so the real Poly Lens keeps working

**Features:**
- Dynamic settings profiles — any Poly headset gets its settings auto-detected
- Battery display in Poly Studio for devices with battery reporting
- Bluetooth headset discovery through USB docks (e.g. Voyager 4320 via Base-M CD)
- Call/mute state tracking from native bridge events
- Product images from Poly Cloud GraphQL API
- Live hotplug detection via background scanner thread

## Native Bridge

Direct ctypes interface to Poly's `libNativeLoader.dylib` + `libPLTDeviceManager.dylib`. Handles all USB HID and Bluetooth communication for DECT/Voyager settings — the same libraries the official Poly Lens uses internally.

```bash
python3 native_bridge.py              # Discover devices and settings
python3 native_bridge.py --set 0x10a medium  # Set Base Ringer Volume
python3 native_bridge.py --get        # Query all settings
```

**72 known setting IDs** covering: sidetone, noise exposure, DECT density, power level, auto-answer, mute alerts, ringtones, volume controls, wearing sensor, online indicator, ANC mode, transparency mode, equalizer, custom button, caller ID, A2DP, quick disconnect, audio bandwidth, anti-startle, and more. Canonical per-device profiles for 217 devices loaded from Poly's DeviceSettings.zip.

**Supports:** Any device that Poly's native library can communicate with — DECT base stations, Bluetooth headsets paired through USB docks, and USB-connected devices.

## LensAPI Client

Direct TCP client for the LensServiceApi protocol — works with both the real Poly Lens service and our LensServer.

```bash
python3 lensapi.py devices                        # List connected devices
python3 lensapi.py discover                        # Full device + settings discovery
python3 lensapi.py settings <device_id>            # Read all settings
python3 lensapi.py get <device_id> "Sidetone"      # Read one setting
python3 lensapi.py set <device_id> "Sidetone" "high"  # Write a setting
python3 lensapi.py dump                            # Export everything as JSON
python3 lensapi.py monitor                         # Watch real-time events
```

## Web Dashboard

```bash
python3 polylens.py                    # Opens browser to localhost:8420
python3 polylens.py --port 9000        # Custom port
python3 polylens.py --no-browser       # Don't auto-open browser
```

- **Devices** — Connected headsets with firmware version, battery, serial number. Auto-refreshes.
- **Updates** — Check Poly Cloud for new firmware, download, and flash with one click.
- **Settings** — Sidetone, EQ, noise limiting, wearing sensor, language, and more per device.
- **Catalog** — Search all Poly firmware online.
- **Firmware Library** — Browse locally cached firmware packages.

Integrates with Clockwork/LensAPI when available, falls back to direct HID.

## CLI Usage

```bash
python3 polytool.py scan              # Discover devices
python3 polytool.py info              # Detailed device info
python3 polytool.py battery           # Battery status
python3 polytool.py updates           # Check for firmware updates
python3 polytool.py update            # Download and flash firmware
python3 polytool.py update --force    # Flash even if up to date
python3 polytool.py catalog           # Search Poly firmware catalog
python3 polytool.py fwinfo <path>     # Analyze a firmware package
python3 polytool.py monitor           # Live auto-refreshing dashboard
python3 menu.py                       # Interactive terminal menu
```

## Fleet Management

Central server for managing headsets across an organization.

**Server** (`polyserver.py`):
```bash
# Requires PostgreSQL
python3 polyserver.py --db postgresql://user:pass@localhost/polytool
```
API keys are auto-generated on first run and printed to the console.

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

**Security:** API key auth (HMAC constant-time comparison), rate limiting (120 req/min per IP), input validation on all endpoints, keys stored with restricted file permissions.

## PolyRemote

Standalone CLI for headset settings. Coexists with Poly Lens — does not modify firmware or the managed client.

```bash
python3 polyremote.py list                                # List devices
python3 polyremote.py set "Sidetone Level" 5              # Change a setting
python3 polyremote.py set "Ringtone Volume" 8 --pid 0xACFF  # Target specific device
python3 polyremote.py get "Sidetone Level"                # Read a setting
python3 polyremote.py dump                                # Dump all settings
python3 polyremote.py batch presets/office_standard.json   # Apply preset
python3 polyremote.py settings                            # List available settings
```

JSON output for automation (auto-enabled when piped):
```bash
python3 polyremote.py list --json | jq '.devices[].name'
python3 polyremote.py dump --json > device_report.json
```

Included presets in `presets/`:
- `office_standard.json` — Standard office config (sidetone 3, ringtone 7, HD Voice on)
- `call_center.json` — High-volume call center (auto-answer on, ringtone 10, noise limiting on)
- `quiet_mode.json` — Minimal audio feedback

## Firmware Flashing

Three protocols, all reverse-engineered from Poly Lens:

**CX2070x EEPROM** (Blackwire 3220) — S-record patches to Conexant EEPROM via HID. Automatically preserves serial number and calibration data at EEPROM offsets 0x0020-0x0083.

**FWU API** (Savi series) — CVM mailbox protocol with LE 16-bit primitive IDs. Tested: 4,915 blocks, 991 KB, 89 seconds. Requires USB reset on macOS before input report reads.

**BladeRunner FTP** (Blackwire 33xx, 7225, 8225, Sync, EncorePro) — File transfer over HID with handshake, block writes, and CRC32 verification.

## Log Monitor

Watch Poly Lens log files in real time with color-coded pattern highlighting:

```bash
python3 monitor_legacyhost.py          # Monitor legacyhost logs
python3 monitor_legacyhost.py --all    # Monitor all log sources (legacyhost, Clockwork, LCS)
python3 monitor_legacyhost.py --raw    # Show all lines, not just interesting patterns
```

Highlights HID reports (green), DFU events (yellow), errors (red), and IPC commands (cyan).

## Protocol Probes

Research tools for reverse engineering Poly USB HID protocols:

```bash
python3 probes/fwu_probe.py --test passive              # Safe read-only scan
python3 probes/fwu_probe.py --test signon                # RID 13 sign-on protocol
python3 probes/fwu_probe.py --test bladerunner           # BladeRunner protocol probe
python3 probes/fwu_probe.py --test enumerate             # Enumerate all HID interfaces
python3 probes/fwu_probe.py --test scan                  # Scan FWU command codes
python3 probes/fwu_probe.py --test all                   # Progressive test suite
python3 probes/dect_settings_probe.py --test read        # Read DECT settings nibbles
python3 probes/dect_settings_probe.py --test scan_cmds   # Scan CVM command families
```

## Project Structure

```
polytool/
├── polytool.py              # Main CLI
├── menu.py                  # Interactive terminal menu
├── polylens.py              # Web dashboard (Flask)
├── lensserver.py            # Drop-in LensService replacement
├── lensapi.py               # LensServiceApi TCP client
├── native_bridge.py         # Direct ctypes interface to Poly native libs
├── lens_settings.py         # Settings profiles and API format conversion
├── device_settings_db.py    # Canonical settings DB from DeviceSettings.zip
├── device_settings.py       # Direct HID settings read/write + translation layer
├── device_identity.py       # Identity preservation during flash
├── fwu_flash.py             # Savi DECT flasher
├── bw_flash.py              # Blackwire 3220 flasher
├── polyserver.py            # Fleet management server
├── polyagent.py             # Fleet workstation agent
├── polyremote.py            # Remote settings CLI
├── clockwork_client.py      # Legacy compatibility wrapper
├── polybus.py               # Native PolyBus library interface
├── monitor_legacyhost.py    # Log file monitor
├── RE_FINDINGS.txt          # Reverse engineering documentation
├── data/
│   ├── Devices.config       # Device PID -> handler mappings
│   ├── DeviceSetting.json   # Settings value database (from LensService)
│   ├── DeviceSettings.zip   # Canonical per-device settings (from Poly Studio LegacyHost)
│   └── settingsCategories.json  # Poly Studio UI settings layout (146 settings)
├── web/
│   ├── index.html           # Dashboard SPA entry point
│   ├── app.js               # Frontend JavaScript
│   └── style.css            # Dashboard styling
├── presets/
│   ├── office_standard.json
│   ├── call_center.json
│   └── quiet_mode.json
└── probes/
    ├── fwu_probe.py         # Unified HID protocol probe
    ├── dect_settings_probe.py  # DECT settings write protocol probe
    └── hid_helpers.py       # Shared HID utilities
```

## Requirements

- Python 3.9+
- `hidapi` — HID device communication
- `requests` — Poly Cloud API
- `flask` — Web dashboard and fleet server
- `pyusb` — USB reset for macOS (Savi flash)
- `psycopg2-binary` — PostgreSQL (fleet server only)
- `rich` — Terminal formatting (optional)

```bash
pip install -r requirements.txt
```

## How It Works

PolyTool talks directly to headsets over USB HID using the same protocols Poly Lens uses internally. Firmware is downloaded from Poly's public CDN and flashed using reverse-engineered protocols from the Poly Lens .NET assemblies and native libraries.

The LensServer component speaks the same TCP protocol as the official Poly Lens Control Service (reverse-engineered from the Poly Studio Electron app's `app.asar`). Poly Studio GUI connects to our server and works exactly as if the official service were running.

The Native Bridge loads Poly's own `libNativeLoader.dylib` and `libPLTDeviceManager.dylib` via ctypes, calling `NativeLoader_SendToNative()` to write settings using the same JSON protocol the official legacyhost uses internally. This gives us full DECT and Bluetooth device support without running any Poly daemons.

Settings are dynamically profiled — when a device is discovered, we query what setting IDs it supports and build a profile that matches Poly Studio's renderer expectations. This means any Poly headset works automatically.

See `RE_FINDINGS.txt` for the complete reverse engineering documentation — register maps, CVM commands, EEPROM layouts, DECT setting IDs (25 captured with values), LensServiceApi protocol details, and protocol specifics for every tested device.

## License

MIT
