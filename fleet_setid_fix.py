"""One-shot SetID writer that survives LegacyDfu's bundle-version smart-skip.

Poly's standard fix-setid path builds a bundle where top-level `version` equals
the setid component's `version`. LegacyDfu interprets the top-level version as
"what firmware the device would end up on," and if it matches the current
component rollup (e.g. usb=1071, headset=1054, tuning=3038) it smart-skips
before touching NVRAM. That blocks us from writing any SetID value whose
string form happens to match a real bundle version.

This helper decouples the two: top-level is a clearly-fake `9999.9999.9999.9999`,
the setid component carries the real target value. LegacyDfu sees a bundle that
doesn't match any known firmware and proceeds straight to the setid write path.

Usage (elevated shell):
    python fleet_setid_fix.py <32-char-serial> <major.minor.revision.build>

Example — align with fleet-pushed target 1071.1054.3038:
    python fleet_setid_fix.py 820F03A0...6EFB 1071.1054.3038.0
"""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from setid_fix import run_legacy_dfu, ensure_legacy_host_running
from devices import discover_devices, try_read_device_info
from device_isolate import isolate
from lcs_control import lcs_temporarily_enabled


def build_decoupled_bundle(pid_hex, setid_value, out_path):
    rules = {
        "version": "9999.9999.9999.9999",
        "type": "firmware",
        "components": [{
            "type": "setid",
            "pid": pid_hex,
            "version": setid_value,
            "description": "fleet_setid_fix: SetID NVRAM rewrite",
            "filename": "",
            "maxDuration": 2,
        }],
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("rules.json", json.dumps(rules, indent=4))


def main():
    if len(sys.argv) != 3:
        print("usage: python fleet_setid_fix.py <serial> <major.minor.revision.build>",
              file=sys.stderr)
        sys.exit(2)
    serial, setid_value = sys.argv[1], sys.argv[2]
    if len(setid_value.split(".")) != 4:
        print(f"setid value must have 4 dot-separated parts, got: {setid_value!r}",
              file=sys.stderr)
        sys.exit(2)

    devs = discover_devices()
    for d in devs:
        try_read_device_info(d)
    matches = [d for d in devs if d.serial == serial]
    if not matches:
        print(f"no connected device with serial {serial!r}", file=sys.stderr)
        print("connected serials:", file=sys.stderr)
        for d in devs:
            print(f"  {d.serial}  ({d.product_name} PID=0x{d.pid:04X})", file=sys.stderr)
        sys.exit(1)
    dev = matches[0]
    print(f"target : {dev.product_name}  VID=0x{dev.vid:04X} PID=0x{dev.pid:04X}")
    print(f"serial : {dev.serial}")
    print(f"tattoo : {dev.tattoo_serial or 'n/a'}")
    print(f"SetID  : {setid_value}")

    tmpdir = Path(tempfile.mkdtemp(prefix="fleet_setid_"))
    bundle = tmpdir / f"setid_0x{dev.pid:04X}.zip"
    build_decoupled_bundle(f"0x{dev.pid:04X}", setid_value, bundle)
    print(f"bundle : {bundle}")

    with lcs_temporarily_enabled(log=print), isolate(dev.serial, dev.vid, dev.pid, log=print):
        if not ensure_legacy_host_running():
            print("LegacyHost DFU pipe never appeared", file=sys.stderr)
            sys.exit(1)
        print("Running LegacyDfu (triggers NVRAM write)...")
        ok, output = run_legacy_dfu(bundle, dev.vid, dev.pid, dev.serial)

    if ok:
        print()
        print("  SUCCESS. Unplug + replug this device's USB cable so LCS re-reads NVRAM.")
        return
    print()
    print("  FAILED. LegacyDfu tail:")
    for line in output.splitlines()[-15:]:
        print(f"    {line}")
    sys.exit(1)


if __name__ == "__main__":
    main()
