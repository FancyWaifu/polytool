# PolyTool

Open-source replacement for the Poly Lens desktop app. CLI + drop-in
`LensService` MITM + REST API for managing Poly/Plantronics USB headsets.

```bash
pip install -r requirements.txt
python3 polytool.py scan          # see what's connected
```

---

## Killer features

- **Fix the FFFFFFFF firmware-version bug.** Some Savi DECT headsets
  ship with unprogrammed SetID NVRAM and Poly Studio chokes on it.
  `polytool fix-setid` writes a valid SetID directly to NVRAM through
  Poly's own LegacyDfu pipeline. Auto-isolates sibling devices when you
  have multiple of the same model so the write actually lands on the
  unit you specified instead of a random one.
  ```bash
  polytool fix-setid <serial>            # one device
  polytool fix-setid all --yes           # every FFFF unit
  ```
  📖 **See [FIXING-FFFF.md](FIXING-FFFF.md)** for the full guide:
  symptoms, prerequisites, troubleshooting, the HTTP API recipe, and a
  technical writeup of how the fix actually works.
- **Make Poly Studio show distinct names + accurate firmware versions.**
  Stock Studio shows two Savi 7320s as identical "Poly Savi 7300 Office
  Series" with blank firmware versions. Our `lensserver.py` MITMs the
  Lens Control Service protocol, synthesizes the missing fields, and
  pipes everything through. `polytool install-service` registers it as
  an auto-start scheduled task.
- **Drop-in firmware updater.** `polytool update-legacy` downloads from
  Poly's CDN and runs the same LegacyDfu/LegacyHost pipeline Studio
  uses — works while Studio is open.
- **HTTP REST API** at `localhost:8080`. Curl-able fix-setid, settings
  read/write, battery, DFU status, native-bridge state.

---

## Quick reference

| Command | What it does |
|---|---|
| `polytool scan` | List connected devices + warn about FFFF state |
| `polytool info <serial\|tattoo\|#>` | Full per-device details |
| `polytool fix-setid <serial>` | Write SetID NVRAM (clears FFFFFFFF) |
| `polytool update-legacy <serial>` | Real firmware update via LegacyDfu |
| `polytool updates` | Check Poly cloud for available firmware |
| `polytool install-service` | Auto-start lensserver MITM at logon |
| `polytool service-{start,stop,status}` | Control the auto-start service |
| `python3 lensserver.py` | Run the MITM in foreground (debugging) |

```bash
# Find a FFFF unit, fix it, verify
polytool scan
# > Warning: 1 device(s) with unprogrammed SetID NVRAM (FFFFs)
polytool fix-setid 820F03A0D2CF42A7BE319796382C6EFB --yes
polytool scan
# > FFFF warning gone
```

---

## HTTP API

Lensserver exposes a REST API at `http://127.0.0.1:8080/api`:

```bash
curl http://127.0.0.1:8080/api/devices                      # list
curl http://127.0.0.1:8080/api/devices/820F03A0             # one device
curl http://127.0.0.1:8080/api/devices/820F03A0/settings    # settings
curl http://127.0.0.1:8080/api/devices/820F03A0/battery     # battery
curl http://127.0.0.1:8080/api/devices/820F03A0/dfu         # firmware status

# Write a setting
curl -X POST http://127.0.0.1:8080/api/devices/820F03A0/settings \
  -H 'Content-Type: application/json' \
  -d '{"name": "audioSensing", "value": "true"}'

# Fix the FFFF bug remotely
curl -X POST http://127.0.0.1:8080/api/devices/820F03A0/fix-setid -d '{}'
# Optional body fields: version (dotted str), isolate (bool), dryRun (bool)
```

See `http_api.py` for the full endpoint list (or hit `/api` for a JSON
index of routes).

---

## Supported devices

Anything with a Poly/Plantronics VID (`0x047F`, `0x05A7`, `0x03F0`).
Detailed handlers exist for:

- **Savi DECT** — 7310/7320/7410/7420, 8200/8210/8220/8410/8420
  (full settings + firmware via `LegacyDfu`)
- **Blackwire** — 3220, 3310/15/20/25, 5xx, 7225, 8225
- **EncorePro** — 310/320/515/525/545
- **Voyager** — 4320, Focus 2, Free 60, Surround 80/85
- **Sync** — 20/40/60
- **Studio** — video bars (firmware only)

For unknown PIDs, polytool reads from a bundled `data/dfu_devices.json`
extracted from Poly's own `dfu.config` (332 PIDs). Brand-new SKUs get
a model name + DFU executor automatically — no code change needed.

---

## How it works

- **`lensserver.py`** speaks the same TCP protocol as Poly's Lens
  Control Service (SOH-delimited JSON over a localhost port advertised
  via `%PROGRAMDATA%\Poly\Lens Control Service\SocketPortNumber`). Poly
  Studio finds it transparently. Background watcher keeps the port file
  claimed even when LCS rewrites it.
- **`setid_fix.py`** forges a minimal `rules.json`-only DFU bundle with
  a `setid` component and feeds it to Poly's `LegacyDfu.exe` via the
  `\\.\pipe\LegacyHostDfuServer` named pipe. That triggers the in-DLL
  SetID write path inside `DFUManager.dll` — same code path Poly uses
  internally, just with our forged input.
- **`device_isolate.py`** handles the multi-device-same-PID routing
  bug in Poly's `SetID::get_device` (it picks by PID alone, not serial).
  Stops LCS → disables sibling HID children via `Disable-PnpDevice` →
  restarts LCS so only the target is visible → runs the DFU → reverses.
  Auto-relaunches the Poly process watchdogs after so Studio still
  works when the dance finishes.
- **`devices.py`** + `data/dfu_devices.json` give every PID a canonical
  model name + DFU handler list, sourced from Poly's own dfu.config.
- **`native_bridge.py`** loads Poly's `PLTDeviceManager.dll` /
  `NativeLoader.dll` via ctypes for live DECT/Voyager state. Spawns a
  32-bit subprocess proxy when running on 64-bit Python.

Full reverse-engineering notes in `RE_FINDINGS.txt`.

---

## Requirements

Python 3.9+, plus:

```
hidapi requests rich
```

`pip install -r requirements.txt` covers everything. Admin privileges
required for `fix-setid` on multi-device hosts (the auto-isolate uses
PowerShell `Disable-PnpDevice`); falls back to a no-op-with-warning
otherwise.

Tested on Windows 11. macOS works for read-only operations and CLI
commands; the MITM is Windows-only.

---

## License

MIT
