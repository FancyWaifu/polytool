# PolyTool

Open-source toolkit for Poly/Plantronics USB headsets on macOS, Linux, and Windows. Replaces the official Poly Lens desktop app with a lightweight web dashboard and CLI tools.

## Features

- **Device Management** — Detect all connected Poly headsets, view firmware, battery, serial
- **Firmware Updates** — Download from Poly Cloud and flash directly over USB HID
- **Device Settings** — Sidetone, EQ, volume, mute, and more via web UI
- **Web Dashboard** — Clean browser UI at `localhost:8420`, no Electron bloat
- **28 Devices Supported** — Blackwire, Savi, Sync, Voyager, EncorePro series

## Quick Start

```bash
pip install hidapi requests flask pyusb rich
python3 polylens.py
```

Opens a web dashboard in your browser. That's it.

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
| `polylens.py` | Web dashboard (start here) |
| `polytool.py` | CLI for scan, info, battery, updates, flash |
| `menu.py` | Interactive terminal menu |
| `fwu_flash.py` | Savi DECT firmware flasher |
| `bw_flash.py` | Blackwire 3220 EEPROM flasher |
| `poly_bridge.py` | MITM proxy for Poly Studio (macOS) |
| `monitor_legacyhost.py` | Poly Lens log monitor |
| `probes/` | HID protocol research tools |

## Web Dashboard

```bash
python3 polylens.py
```

Tabs:
- **Devices** — Connected headsets with firmware, battery, serial. Auto-refreshes.
- **Firmware Updates** — Check Poly Cloud, download, and flash with one click.
- **Settings** — Sidetone, EQ, noise limiting, wearing sensor, language, etc.
- **Catalog** — Search all Poly firmware online.

## CLI Usage

```bash
# Scan for devices
python3 polytool.py scan

# Check for firmware updates
python3 polytool.py updates

# Flash firmware
python3 polytool.py update

# Device info
python3 polytool.py info

# Battery status
python3 polytool.py battery

# Interactive menu
python3 menu.py
```

## Firmware Flashing

Three protocols are implemented, all reverse-engineered from Poly Lens:

**CX2070x EEPROM** (Blackwire 3220) — Writes S-record patches to Conexant EEPROM via HID. Preserves device serial number and calibration data automatically.

**FWU API** (Savi series) — CVM mailbox protocol with LE 16-bit primitive IDs over USB HID. Requires `pyusb` for USB reset before flash on macOS.

**BladeRunner FTP** (Blackwire 33xx, Sync, EncorePro) — File transfer protocol over HID with handshake, block writes, and CRC32 verification.

All flash paths preserve device identity (serial number, calibration) so headsets remain compatible with Poly Lens after updates.

## Device Identity Preservation

Firmware update files contain generic placeholder values that overwrite device-unique EEPROM data (serial numbers, calibration bytes). PolyTool automatically backs up and restores this data during flash, so headsets remain fully functional in Poly Lens and other management tools.

## Requirements

- Python 3.9+
- `hidapi` — HID device communication
- `requests` — Poly Cloud API
- `flask` — Web dashboard
- `pyusb` — USB reset for Savi flash (macOS)
- `rich` — Terminal formatting (optional)

## How It Works

PolyTool talks directly to headsets over USB HID — the same protocols Poly Lens uses internally. No cloud account, no background services, no auto-launching daemons. Start it when you need it, close it when you don't.

Firmware is downloaded from Poly's public CDN (no authentication required) and flashed using reverse-engineered protocols from `libDFUManager.dylib` and the Poly Lens DFU executors.

## License

MIT
