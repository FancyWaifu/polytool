# PolyTool — Claude Code Context

## What This Is
Open-source replacement for Poly Lens desktop app. Manages Poly/Plantronics USB headsets — settings, firmware, fleet management. The centerpiece is `lensserver.py` which replaces Poly's Lens Control Service so the Poly Studio GUI connects to our server instead.

## Architecture
- **lensserver.py** — TCP server speaking LensServiceApi protocol (SOH-delimited JSON). Poly Studio connects to it automatically via port file at ~/Library/.../SocketPortNumber (macOS) or %PROGRAMDATA%\Poly\...\SocketPortNumber (Windows)
- **native_bridge.py** — ctypes interface to Poly's own libNativeLoader + libPLTDeviceManager dylibs/DLLs. Handles DECT/Voyager/BT device discovery and settings writes. On Windows, detects 32-bit DLL vs 64-bit Python and spawns a subprocess proxy (native_bridge_worker.py)
- **device_settings.py** — Direct HID read/write for CX2070x (register writes) and BladeRunner (GET/SET_SETTING protocol). Translation layer maps Poly Studio setting names to HID register names
- **lens_settings.py** — Per-device settings profiles. Settings must match IDs in data/settingsCategories.json or Poly Studio won't render them

## Key Technical Details
- LensServiceApi uses SOH byte (0x01) as message delimiter, NOT newline
- Setting values in DeviceSettings response MUST include `meta` object or SettingItem component returns null (the renderer bug we fixed)
- Native bridge requires exclusive USB access — kill Poly Studio before starting lensserver
- Port file is saved/restored on shutdown so real Poly Lens keeps working
- 34 known DECT/Voyager setting hex IDs in ALL_SETTING_DEFS (native_bridge.py)
- Dynamic profiles: native bridge queries device capabilities, builds settings list from what it reports

## Device Families
- **cx2070x** (BW 3220): 2 settings, direct HID register writes
- **bladerunner** (BW 33xx/7225/8225, EncorePro, Sync): 11 settings, HID GET/SET_SETTING
- **dect** (Savi 7300/8200): 30 settings via native bridge
- **voyager_bt** (Voyager 4320 etc.): 18 settings via native bridge
- **voyager_base** (Base-M CD): 6 settings via native bridge

## Native Library Paths
- macOS: /private/tmp/PolyStudio.app/.../LegacyHostApp.app/Contents/Components/
- Windows: C:\Program Files\Poly\Poly Studio\LegacyHost\

## Common Issues
- WinError 193: DLL architecture mismatch (32-bit DLL, 64-bit Python) — native_bridge handles this with subprocess proxy
- StartNativeBridge symbol not found: expected on macOS, NativeLoader_Init already starts scanning
- libc++ mutex crash on exit: cosmetic, native library cleanup race condition
- Settings not rendering: check meta object is included in DeviceSettings values
- Device disappearing: USB scanner removes BT devices — check _native_id preservation logic

## Data Files
- data/Devices.config — 57 device PIDs with handler types
- data/DeviceSetting.json — 50 settings with internal keys → display values
- data/settingsCategories.json — 146 UI setting definitions from Poly Studio renderer

## Testing
- `python3 lensserver.py` then open Poly Studio
- `python3 lensserver.py --verbose` for debug output
- `python3 native_bridge.py` for standalone native bridge test
- `python3 lensapi.py discover` to query the running server
- `python3 polytool.py scan` for basic USB device detection
