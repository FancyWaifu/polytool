# Fixing the FFFFFFFF Firmware Version Bug

If your Poly headset shows up in Poly Studio with a blank or `FFFFFFFF`
firmware version, doesn't get firmware updates offered, or appears with
broken capability gates — you've hit the **unprogrammed SetID NVRAM**
bug. This guide walks through what it is, why it happens, and how to
fix it permanently with `polytool`.

> **One-line fix (most users):**
> ```bash
> python3 polytool.py fix-setid <serial> --yes
> ```

---

## Symptoms

- Poly Studio shows the device but firmware version is blank or
  reads `ffff.ffff.ffff.ffff`
- "Update available" button never appears even when newer firmware
  exists in Poly's cloud
- Capability flags (call control, mute, settings) are inconsistent or
  missing
- Device works for audio but is otherwise treated as broken by the
  Lens Control Service

Run `polytool scan` to confirm — affected devices are flagged in a
warning block:

```
Warning: 1 device(s) with unprogrammed SetID NVRAM (FFFFs):
    - Poly Savi 7320 (S/N3HRG5M) (0x047F:0xAC28)
      FirmwareVersion='ffff.ffff.ffff.ffff'
    Fix:  polytool fix-setid       (fast, ~10 sec)
          polytool update-legacy   (full firmware update, ~10 min, also fixes it)
```

---

## What's actually wrong

SetID is a 4-tuple of 16-bit values stored in a small NVRAM region on
the headset base. Poly's manufacturing process is supposed to program
it with a real value during assembly. **Some manufacturing batches
shipped with the SetID region unprogrammed** — that means the bytes
read back as the flash-erase default `0xFFFF`.

The Lens Control Service reads SetID as part of the device's overall
firmware version. When it sees `0xFFFF` it forwards that to Poly Studio,
which treats it as a literal version number `ffff.ffff.ffff.ffff`.
Studio's update logic compares this to the cloud's actual versions,
declares the device "newer than anything available," and hides the
update button. Settings rendering also breaks because Studio's
capability gating depends on a parseable version.

**It's not corruption from a bad firmware update.** It's a factory step
that was skipped. The headset is otherwise healthy.

### Affected models

Mostly **Savi DECT** bases (AC27, AC28, AC2A, AC2B, etc.) on
firmware ≤ 10.71. Other DECT and Voyager devices that include a
`setid` component in their DFU bundles (per Poly's own `dfu.config`)
are also susceptible. Run `polytool scan` to detect.

### Why Poly hasn't fixed it

The newer Savi firmware (10.82+) silently dropped `setId` from the
device's firmware-version API response. That hides the FFFFs from LCS,
so Studio stops choking — but the underlying NVRAM is still
unprogrammed, and the new firmware also **rejects setid writes**
(`code:9` from LegacyDfu). So if your unit auto-updates to 10.82+ first,
the FFFFs disappear from LCS but you can never write a real value.

**Run `polytool fix-setid` BEFORE any firmware update** if you have
both options. The fix-setid path only works on firmware ≤ 10.71.

---

## How to fix it

### Prerequisites

- Windows (the fix uses Poly's `LegacyDfu.exe` and `LegacyHost.exe`)
- Poly Lens Control Service installed (provides `LegacyDfu.exe`)
- Run polytool **from an elevated/admin shell** if you have multiple
  devices of the same model — needed for the auto-isolate step

```bash
# Verify Poly Lens is installed
ls "C:/Program Files/Poly/Lens Control Service/eDfu/LegacyDfu.exe"
```

### Step 1: Identify the affected device

```bash
python3 polytool.py scan
```

The warning block will list each unit with its serial and tattoo
(printed on the headset label). Copy the 32-character serial.

### Step 2: Run fix-setid

```bash
python3 polytool.py fix-setid <serial> --yes
```

Example with a real serial:

```bash
python3 polytool.py fix-setid 820F03A0D2CF42A7BE319796382C6EFB --yes
```

You'll see output like:

```
Poly Savi 7320 (S/N3HRG5M)  (0x047F:0xAC28)  serial=820F03A0...
    Target: VID=0x047F PID=0xAC28 serial=820F03A0...
    SetID to write: 0001.0000.0000.0001  (major.minor.revision.build)
    Forged bundle: ...\polytool_setid_xxx\setid_0xac28.zip
    isolate: hiding sibling serial=049160FE... (7 HID children)
    isolate: stopping Poly Lens Control Service...
    isolate: disabling 13 HID interfaces...
    isolate: 13/13 disabled successfully
    isolate: restarting Poly Lens Control Service...
    LegacyHost DFU pipe is up
    Running LegacyDfu.exe (this triggers the actual NVRAM write)...
    isolate: re-enabling 13 HID interfaces...
    isolate: done
  OK: SetID write completed.
```

Total time: ~5 seconds on a single-device host, ~25-30 seconds when
auto-isolation has to stop and restart LCS.

### Step 3: Verify

```bash
python3 polytool.py scan
```

The FFFF warning should be gone. Re-dock the headset for Poly Studio's
cache to refresh and the firmware version will display normally.

### Fixing multiple devices

Just call fix-setid once per serial. Auto-isolate handles the
multi-device routing automatically:

```bash
python3 polytool.py fix-setid 820F03A0D2CF42A7BE319796382C6EFB --yes
python3 polytool.py fix-setid 9E65EB422A9843069B8ECE06C0778727 --yes
```

Or use `all` to process every detected FFFF unit:

```bash
python3 polytool.py fix-setid all --yes
```

### Fixing remotely via HTTP API

If `lensserver.py` is running (`polytool service-start` or
`polytool install-service`), POST to the API:

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{}' \
  http://127.0.0.1:8080/api/devices/820F03A0/fix-setid
```

Returns `{"success": true, ...}` with the full execution log. The
`deviceId` in the URL is the 8-character prefix from `polytool scan`.

Optional body fields:

```json
{
  "version": "0001.0000.0000.0001",   // dotted major.minor.revision.build
  "isolate": true,                     // auto-isolate sibling devices
  "dryRun": false                      // build bundle but don't write
}
```

---

## Troubleshooting

### `LegacyDfu did not report 'DFU Complete: 100'` / `code:9`

Most common cause: the device is running firmware 10.82+ which rejects
setid writes. Check the firmware version with `polytool info <serial>`:

- `usb=10.71` or older → setid write should work
- `usb=10.82` or newer → too late, the fix path is closed

If you have multiple devices of the same model and the failed one is
on old firmware, the routing bug may have hit — make sure you're
running from an **elevated shell** so auto-isolate can disable the
sibling devices.

### `isolate: WARNING — not running elevated`

The auto-isolate step needs admin to call `Disable-PnpDevice`. Open a
new terminal as Administrator and re-run. Without isolation, the fix
may write to the wrong device when multiple of the same model are
connected.

### `LegacyHost.exe could not start or DFU pipe never appeared`

Poly Lens Control Service isn't installed, or it's been disabled. Make
sure the service exists:

```bash
sc query "Poly Lens Control Service"
```

If it shows `STOPPED`, start it: `net start "Poly Lens Control Service"`.
If it doesn't exist at all, install Poly Lens from
[poly.com/us/en/support/downloads-apps](https://www.poly.com/us/en/support/downloads-apps).

### Poly Studio doesn't refresh after the fix

LCS caches device state. Either:
- Re-dock the headset (unplug from cradle, replace), or
- Restart Poly Studio (close from system tray, reopen)

Both force LCS to re-read the device and pick up the new SetID value.

### Fix succeeded but headsets disappear in Poly Studio

The auto-isolate restarts LCS, which kills the per-user watchdog
process. Newer polytool versions auto-respawn the watchdogs after the
fix completes. If you're on an older version, manually run:

```bash
"C:/Program Files/Poly/Poly Studio/ProcessWatchdog/PolyLensProcessWatchdog.exe" \
  "C:/Program Files/Poly/Poly Studio/LegacyHost/LegacyHost.exe"
```

Or just log out and back in — the watchdog auto-launches at logon via
the `com.poly.lens.client.watchdog.lh` Run-key entry.

---

## How the fix actually works

For the curious. The `fix-setid` command:

1. **Forges a minimal DFU bundle.** Just a `rules.json` zipped up — no
   firmware files. The rules.json has a single component of type
   `setid` with our chosen version string:
   ```json
   {
     "version": "0001.0000.0000.0001",
     "type": "firmware",
     "components": [{
       "type": "setid",
       "pid": "0xAC28",
       "version": "0001.0000.0000.0001",
       "filename": "",
       "maxDuration": 2
     }]
   }
   ```

2. **Auto-isolates other devices** (when multiple same-PID devices are
   present). Poly's `SetID::get_device` in `DFUManager.dll` routes by
   PID alone — even when our `--serial` flag specifies a target. With
   multiple AC28s connected it would write to a random one, often the
   wrong one. So we:
   - Stop the Lens Control Service (releases its HID handles)
   - Disable sibling HID children via `Disable-PnpDevice`
   - Restart LCS — now it only sees the target device
   - Run the DFU
   - Reverse: stop LCS, re-enable siblings, restart LCS, respawn the
     Poly watchdog processes

3. **Invokes `LegacyDfu.exe`** with the forged bundle. LegacyDfu
   connects to `\\.\pipe\LegacyHostDfuServer`. LegacyHost recognizes
   the setid component type and routes it to `DFUManager.dll`'s
   built-in SetID handler, which writes the version string to the
   device's NVRAM through PLTDeviceManager.

4. **Confirms via readback.** The SetID handler reads the value back
   from NVRAM after the write. Success = "DFU Complete: 100".

The whole NVRAM write is a single 4-tuple of 16-bit values — small
enough to be near-instant, but the LegacyDfu/LegacyHost pipeline takes
~5 seconds end to end because of pipe handshake overhead.

Full reverse-engineering notes are in `RE_FINDINGS.txt` (sections 11
and 11.A).

---

## What this fix DOESN'T change

- **Doesn't update firmware.** Use `polytool update-legacy` for that.
- **Doesn't change the device's serial number, MAC, or pairing.** Only
  the SetID NVRAM region.
- **Doesn't affect the headset itself** — only the base/dock. The
  paired headset's NVRAM is untouched.
- **Survives reboots, USB unplugs, and LCS restarts.** It's a
  permanent NVRAM write.
- **Doesn't survive a factory reset.** A full reset would re-erase the
  NVRAM region. Just re-run `fix-setid`.

---

## Related commands

| Command | Purpose |
|---|---|
| `polytool scan` | Detect FFFF devices |
| `polytool info <serial>` | Full per-device details incl. firmware components |
| `polytool fix-setid <serial>` | The fix |
| `polytool update-legacy <serial>` | Full firmware update (also clears FFFF as side-effect, but takes 10 min) |
| `polytool install-service` | Auto-start lensserver MITM so Studio shows distinct names + versions |

---

## License

MIT — same as the rest of polytool.
