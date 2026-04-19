"""Retry fix-setid every minute until it succeeds. Uses LegacyDfu's own
exit code/output as the trigger - if it complains about battery, wait
60s and try again. Any non-battery failure stops the loop.

Replaces the native-bridge battery polling approach (battery readings
were stuck at -1 because the headset wasn't reachable, but LegacyDfu
itself can talk to it through the base)."""
import sys, time, subprocess

SERIAL = "377CF5FD4D5A4E1CA151FF7FAA3A5E8A"
INTERVAL = 60  # seconds between attempts
MAX_ATTEMPTS = 60  # ~1 hour cap


def attempt(n):
    print(f"[attempt #{n}] {time.strftime('%H:%M:%S')}  fix-setid",
          flush=True)
    r = subprocess.run(
        ["python3", "polytool.py", "fix-setid", SERIAL, "--yes", "--verbose"],
        capture_output=True, text=True, timeout=300,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if "OK: SetID write completed" in out:
        return "success", out
    if ("Battery level on the device too low" in out or
            "code:5" in out):
        return "battery", out
    return "other", out


def main():
    print(f"[*] Will retry fix-setid every {INTERVAL}s until it succeeds.",
          flush=True)
    print(f"    target serial: {SERIAL}", flush=True)
    for n in range(1, MAX_ATTEMPTS + 1):
        kind, out = attempt(n)
        if kind == "success":
            print("[+] SUCCESS - SetID written.", flush=True)
            return 0
        if kind == "battery":
            print(f"[battery] LegacyDfu still says too low - waiting {INTERVAL}s",
                  flush=True)
            time.sleep(INTERVAL)
            continue
        # Other failure - stop and surface the error
        print("[!] Failed for non-battery reason. Last 8 error-ish lines:",
              flush=True)
        for line in out.splitlines():
            if any(k in line for k in ("code:", "error:", "FAILED")):
                print(f"    {line.strip()[:160]}", flush=True)
        return 1
    print(f"[!] Hit max attempts ({MAX_ATTEMPTS}) - giving up", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
