"""Poll a Poly headset's battery and auto-retry fix-setid when it crosses
the LegacyDfu battery threshold. Standalone helper; not part of polytool's
public CLI."""
import sys, time, subprocess
sys.path.insert(0, ".")
from native_bridge import NativeBridge

TARGET_TATTOO = "S/N2UGHYA"
TARGET_SERIAL = "377CF5FD4D5A4E1CA151FF7FAA3A5E8A"
THRESHOLD = 35
POLL_SECONDS = 30


def main():
    print(f"[*] Polling {TARGET_TATTOO} battery, retrying fix-setid at >= {THRESHOLD}%",
          flush=True)

    b = NativeBridge()
    b.start()
    time.sleep(8)
    attempts = 0
    while True:
        try:
            devs = b.get_devices() or {}
        except Exception as e:
            print(f"[!] get_devices error: {e}", flush=True)
            break
        target_nid = None
        for nid, d in devs.items():
            sn = d.get("serialNumber", []) or []
            base = next((s.get("value", {}).get("base", "") for s in sn
                         if s.get("type") == "tattoo"), "")
            if base == TARGET_TATTOO:
                target_nid = nid
                break
        if not target_nid:
            print(f"[!] {TARGET_TATTOO} not visible - is it plugged in?", flush=True)
            break
        bat = b.get_battery(target_nid) or {}
        level = bat.get("level", -1)
        charging = bat.get("charging", False)
        docked = bat.get("docked", False)
        pct = (level * 20) if 0 <= level <= 5 else level
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] battery: {pct}% (raw={level}) charging={charging} docked={docked}",
              flush=True)
        if pct >= THRESHOLD:
            attempts += 1
            print(f"[*] Battery {pct}% >= {THRESHOLD}% - attempt #{attempts}: fix-setid",
                  flush=True)
            b.stop()
            time.sleep(3)
            r = subprocess.run(
                ["python3", "polytool.py", "fix-setid", TARGET_SERIAL,
                 "--yes", "--verbose"],
                capture_output=True, text=True, timeout=300,
            )
            out = (r.stdout or "") + (r.stderr or "")
            if "OK: SetID write completed" in out:
                print("[+] SUCCESS - SetID written.", flush=True)
                return 0
            if ("Battery level on the device too low" in out or
                    "code:5" in out):
                print("[!] LegacyDfu still says battery too low - waiting more",
                      flush=True)
            else:
                print("[!] Failed for non-battery reason:", flush=True)
                for line in out.splitlines():
                    if any(k in line for k in ("code:", "error:", "FAILED")):
                        print(f"    {line.strip()[:160]}", flush=True)
                return 1
            b = NativeBridge()
            b.start()
            time.sleep(8)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
