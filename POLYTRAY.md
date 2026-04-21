# PolyTray — Install and Use Guide (Windows)

PolyTray is a small Windows desktop app that **auto-detects and fixes the FFFFFFFF firmware-version bug** on Poly DECT headsets (Savi 7320 / AC28 and siblings). It sits in a window, watches USB, and when a headset shows up with unprogrammed SetID NVRAM (`firmwareVersion = FFFF`), it offers to write a valid SetID so Poly Studio will start displaying firmware and offering updates again.

Windows only. Will not run on macOS or Linux.

---

## Table of Contents

1. [What the FFFF Bug Is](#1-what-the-ffff-bug-is)
2. [Requirements](#2-requirements)
3. [Install Poly Studio + Lens Control Service](#3-install-poly-studio--lens-control-service)
4. [Install Python](#4-install-python)
5. [Get PolyTool](#5-get-polytool)
6. [Install Python Dependencies](#6-install-python-dependencies)
7. [Run PolyTray](#7-run-polytray)
8. [Using PolyTray](#8-using-polytray)
9. [Battery-Low Handling](#9-battery-low-handling)
10. [Troubleshooting](#10-troubleshooting)
11. [Uninstall](#11-uninstall)

---

## 1. What the FFFF Bug Is

Some Savi DECT headsets ship from the factory with the **SetID NVRAM region unprogrammed** — all bytes are `0xFFFF`. The Poly Cloud firmware bundle for these devices doesn't include a `setid` component, so the normal Poly Lens update flow never writes it.

The symptom: Poly Studio reads `firmwareVersion.setId = {build:ffff, major:ffff, ...}` and gives up. You see:

- Blank firmware version in Poly Studio
- No firmware updates offered
- Capability gates (like wideband audio, settings panels) disabled

PolyTray fixes it by writing a synthetic valid SetID to NVRAM using Poly's own `LegacyDfu.exe` and `LegacyHost.exe`. Once written, Poly Studio starts reporting the device correctly.

---

## 2. Requirements

| Item | Why |
|------|-----|
| **Windows 10 or 11** | PolyTray is Windows-only — it talks to `LegacyDfu.exe` and the `\\.\pipe\LegacyHostDfuServer` named pipe. |
| **Python 3.9+** | Tkinter GUI. The python.org Windows installer includes Tkinter by default. |
| **Poly Studio + Lens Control Service** | PolyTray re-uses their native binaries (`LegacyDfu.exe`, `LegacyHost.exe`, `PLTDeviceManager`). Install from https://lens.poly.com/download |
| **Affected Poly headset** | Savi 7320, Savi 8220, AC28-family DECT bases. Any Poly headset currently showing blank/`FFFF` firmware in Poly Studio. |
| **Firmware < 10.82 on the headset** | Poly closed the SetID write path starting in firmware 10.82. Newer firmware can't be fixed this way. |
| **~50% battery on the headset** | The SetID write runs a short DFU cycle; LegacyDfu refuses to start below its battery threshold. PolyTray auto-retries while you charge. |

---

## 3. Install Poly Studio + Lens Control Service

1. Go to **https://lens.poly.com/download**
2. Download **Poly Lens Desktop** for Windows
3. Run the installer as Administrator and accept the defaults — this installs both:
   - **Poly Studio** (includes `LegacyHost.exe`)
   - **Poly Lens Control Service** (includes `LegacyDfu.exe`)
4. Launch Poly Studio once so it finishes first-run setup, then close it

PolyTray expects the standard install paths:

```
C:\Program Files\Poly\Lens Control Service\eDfu\LegacyDfu.exe
C:\Program Files\Poly\Poly Studio\LegacyHost\LegacyHost.exe
C:\ProgramData\Poly\Lens Control Service\        (device cache)
```

If you installed to a non-default location, PolyTray also checks `C:\Program Files (x86)\...`. Other paths will fail to start the fix.

> **Don't uninstall the Lens Control Service.** PolyTray needs it running. Unlike the main LensServer flow in the README, PolyTray **coexists** with Poly's stock service.

---

## 4. Install Python

1. Download Python 3.11 or newer from https://www.python.org/downloads/windows/
2. Run the installer
3. **Check "Add python.exe to PATH"** on the first screen
4. Click *Install Now*

Verify in a new PowerShell or Command Prompt window:

```powershell
python --version
pip --version
```

Both should print a version number. Tkinter is bundled with the python.org installer — no extra step needed.

---

## 5. Get PolyTool

PolyTray lives inside the `polytool` repo (it imports `devices.py`, `setid_fix.py`, and `native_bridge.py`). You need the whole repo, not just the one file.

**Option A — Git:**

```powershell
git clone https://github.com/FancyWaifu/polytool.git
cd polytool
```

**Option B — Download ZIP:**

1. Open the repo page on GitHub
2. Click *Code → Download ZIP*
3. Extract to `C:\polytool` (or anywhere you like)
4. `cd C:\polytool` in PowerShell

---

## 6. Install Python Dependencies

From inside the `polytool` folder:

```powershell
pip install -r requirements.txt
```

The GUI itself only needs Tkinter (built in), but PolyTray imports from `devices.py` and `native_bridge.py`, which need:

- `hidapi` — USB HID for device discovery
- `requests` — Poly Cloud lookups
- `pyusb` — USB resets
- `rich` — optional terminal formatting

You can skip `flask` and `psycopg2-binary` if you're not running the web dashboard or fleet server.

---

## 7. Run PolyTray

From the `polytool` folder:

```powershell
python polytray.py
```

A window titled **"PolyTray — FFFF Auto-Fix"** opens:

```
┌─────────────────────────────────────────────────────────┐
│ PolyTray — FFFF Auto-Fix                                │
│ Plug in a Poly headset. FFFF state will be detected and │
│ fixed automatically.                                    │
├─────────────────────────────────────────────────────────┤
│ ☐ Auto-fix without prompting (skip the confirmation)    │
├─────────────────────────────────────────────────────────┤
│ Device              Tattoo    Battery    Firmware  Status│
│ Savi 7320 Office    049160..  80%        10.75     OK    │
│ Savi 7320 Office    04AB12..  45%        FFFF.FF   FFFF… │
├─────────────────────────────────────────────────────────┤
│ 2 device(s) connected | polling every 2s                │
└─────────────────────────────────────────────────────────┘
```

Leave it running. It polls every 2 seconds and will detect any headset you plug in.

---

## 8. Using PolyTray

### Auto-detection and fix

When PolyTray sees a device in FFFF state, it either:

- Pops a **"FFFF Detected on Poly Headset"** dialog asking whether to fix it (default), or
- Runs the fix immediately if you ticked **"Auto-fix without prompting"**

Click **Yes** to start. The fix takes about 30 seconds:

1. PolyTray builds a forged firmware bundle containing only a `setid` component
2. Hands it to `LegacyDfu.exe`
3. `LegacyDfu` connects to `LegacyHost.exe` via `\\.\pipe\LegacyHostDfuServer`
4. `LegacyHost` writes the SetID through `PLTDeviceManager` into the headset's NVRAM

When it succeeds you'll see a **"Fixed!"** dialog with the version that was written. **Re-dock the headset** so Poly Studio refreshes — the firmware column will populate and updates will start appearing.

### Fix a specific device manually

Right-click any row in the device list → **"Fix this device now"**. Useful for:

- Re-running the fix if the first attempt failed
- Fixing an OK-looking device that Poly Studio is still displaying wrong

### Auto-fix toggle

If you're provisioning multiple headsets in a row, tick **"Auto-fix without prompting"** at the top. PolyTray will silently fix every FFFF device as it's plugged in. Leave it unchecked for one-off use.

### Status column legend

| Status | Meaning |
|--------|---------|
| `OK (setid programmed)` | Device is healthy. Nothing to do. |
| `FFFF DETECTED — needs fix` | Unprogrammed SetID. Fix is available. |
| `Fixing…` | Fix in progress. Don't unplug. |
| `Waiting for battery (attempt N, retry in Ns)` | Battery was too low. Auto-retrying every 60s — charge the headset. |

---

## 9. Battery-Low Handling

`LegacyDfu` refuses to write SetID if the headset battery is below its internal threshold (around 20%). When that happens:

- The fix fails silently — **no error dialog**
- PolyTray shows a one-time **"Charge the Headset"** notice
- The device moves into *Waiting for battery* state in the list
- PolyTray retries the fix automatically **every 60 seconds**

Place the headset in its charging cradle. It takes about **10 minutes** to reach a usable level. Once the retry succeeds you'll get the normal **"Fixed!"** dialog.

You don't need to click anything during this — just charge and wait.

---

## 10. Troubleshooting

**"PolyTray is Windows-only."**
- You ran it on macOS or Linux. Not supported. Use a Windows machine.

**`ModuleNotFoundError: No module named 'hidapi'` (or `requests`, `pyusb`)**
- Run `pip install -r requirements.txt` again, from inside the `polytool` folder.

**`LegacyDfu.exe not found`**
- Poly Lens Control Service isn't installed, or it's in a non-standard path. Reinstall from https://lens.poly.com/download.

**Dialog says "FFFF Detected" but clicking Yes fails instantly**
- `LegacyHost.exe` isn't running. Open Poly Studio once to launch it, then close Studio but leave LegacyHost running in the background. Retry.

**"Firmware too new (10.82+ closes the SetID write path)" in the failure dialog**
- Poly closed the write path. There's no workaround — you'd need to downgrade firmware first, and Poly's cloud doesn't offer that for most devices.

**Battery shows `n/a` for every device**
- The native bridge takes ~8 seconds to spin up on first launch. If it stays `n/a`, your headset model doesn't report battery through the native bridge (non-DECT devices). Fix will still work — battery display is just informational.

**Fix succeeds but Poly Studio still shows blank firmware**
- Re-dock the headset (unplug it from the base, put it back). Poly Studio re-reads the device on dock events.
- If Poly Studio is open, close and reopen it.

**"Fix crashed" with a traceback**
- Paste the traceback into a GitHub issue. Most common cause: LegacyHost.exe crashed mid-pipe. Restart Poly Studio and try again.

**Multiple headsets of the same model plugged in, fix hits the wrong one**
- Unplug all but the one you want to fix, then plug it back in. PolyTray's sibling-isolation logic is good but not perfect with identical PIDs.

**PolyTray window hides behind Poly Studio**
- PolyTray uses `topmost=True` only during the alert. Click its taskbar icon to bring it forward, or enable Auto-fix so you don't need to interact with it.

---

## 11. Uninstall

PolyTray writes nothing outside the `polytool` folder and creates no services or registry keys.

1. Close the PolyTray window (top-right X)
2. Delete the `polytool` folder if you want it gone

Poly Studio and the Lens Control Service are untouched — they keep working exactly as before.

---

## Appendix — What PolyTray Does Under the Hood

For the curious. Walk through is in `setid_fix.py`:

1. **Discover** — `devices.discover_devices()` enumerates Poly USB HID devices.
2. **Diagnose** — `diagnose_setid()` reads the LCS device cache from `C:\ProgramData\Poly\Lens Control Service\` and checks each device's `firmwareVersion.setId` block. If any field is `ffff`, the device is flagged.
3. **Forge bundle** — Builds a minimal firmware zip whose `rules.json` contains only:
   ```json
   {"type":"setid","pid":"0xAC28","version":"0001.0000.0000.0001",
    "filename":"","maxDuration":2}
   ```
4. **Invoke DFU** — Runs `LegacyDfu.exe` with the forged bundle. It connects to `LegacyHost.exe` over `\\.\pipe\LegacyHostDfuServer`.
5. **Write NVRAM** — LegacyHost recognizes the `setid` component and calls through `PLTDeviceManager` to write the version bytes to the headset's SetID NVRAM region.
6. **Verify** — PolyTray re-reads the cache; the FFFF is gone.

Reverse-engineered via Frida traffic capture against the running Poly Lens Control Service. See `RE_FINDINGS.txt` for protocol details.

---

**License:** MIT — same as PolyTool.
