"""Resolve the best SetID NVRAM value to write for a given device.

Why this exists: a SetID write picks a string that LCS reports as
FirmwareVersion. If that string doesn't match what Poly Studio considers
"current" for the device, Studio shows an Update Software button — even
when the device is fine. The right value depends on how the host is
managed:

  * Tenant-enrolled (Poly Lens Manager): Studio compares against the
    tenant-pushed target (in PostponeDFUData). Writing a SetID that
    matches that target hides the button.
  * Standalone: Studio compares against Poly Cloud's latest published
    firmware. Writing a SetID that matches cloud latest hides the button.

SetID NVRAM stores four 16-bit values. Cloud/fleet version strings are
usually three-part ("1082.1065.3038"). We zero-pad to four parts
("1082.1065.3038.0"). Poly Studio's comparator zero-pads the target at
compare time too, so the two end up equal.

Resolution order:
  1. Fleet target from local LCS PostponeDFUData (covers the tenant case)
  2. Cloud latest from Poly's public GraphQL catalog (covers standalone)
  3. Fallback to the historical default "0001.0000.0000.0001"
"""

import json
import re
from pathlib import Path


LCS_DIR = Path(r"C:\ProgramData\Poly\Lens Control Service")
_TENANT_FILES = ("TenantId", "AssignedHub", "OnboardingStatus")
DEFAULT_FALLBACK = "0001.0000.0000.0001"


def is_fleet_enrolled() -> bool:
    """True when LCS is onboarded to a Poly Lens Manager tenant.

    The three files are created by the LCS onboarding flow and persist until
    the machine is explicitly un-enrolled. Presence of all three is a strong
    signal that policy (including pinned firmware targets) is being pushed.
    """
    return all((LCS_DIR / f).exists() for f in _TENANT_FILES)


def read_fleet_target(serial: str):
    """Return the tenant-pushed DFU target version for `serial`, or None.

    LCS writes PostponeDFUData whenever the cloud announces a new firmware
    target for a device. Each entry includes the bundle Version we're being
    asked to install. That's the string Studio compares against.
    """
    p = LCS_DIR / "PostponeDFUData"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    entry = data.get(f"PLT_{serial}") or data.get(serial)
    if not entry:
        return None
    v = (entry.get("Version") or "").strip()
    return v or None


def query_cloud_latest(pid: int):
    """Return Poly Cloud's latest published firmware version for `pid`.

    Returns None on any failure (no network, no requests installed, GraphQL
    error). Caller should fall back to the next resolution step.
    """
    try:
        from firmware import PolyCloudAPI
    except Exception:
        return None
    try:
        cloud = PolyCloudAPI()
        query = """query ($pid: ID!) {
          availableProductSoftwareByPid(pid: $pid) {
            productBuild { version }
          }
        }"""
        data = cloud._graphql(query, {"pid": f"{pid:x}"})
        sw = (data or {}).get("availableProductSoftwareByPid") or {}
        pb = sw.get("productBuild") or {}
        v = (pb.get("version") or "").strip()
        return v or None
    except Exception:
        return None


def pad_to_setid(version: str) -> str:
    """Pad a dotted version string to the 4-part form SetID NVRAM requires.

    "1082.1065.3038"   -> "1082.1065.3038.0"
    "1082.1065.3038.0" -> "1082.1065.3038.0"
    "1082"             -> "1082.0.0.0"
    """
    parts = [p for p in version.split(".") if p != ""]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def _looks_like_version(s: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+){0,3}", s or ""))


def resolve_setid_target(serial: str, pid: int):
    """Pick the SetID string to write and return (value, source_label).

    value  — 4-part dotted string ready to pass to fix_setid(version=...)
    source — one of: "fleet", "cloud", "fallback". For UI and logging.
    """
    if is_fleet_enrolled():
        fleet = read_fleet_target(serial)
        if fleet and _looks_like_version(fleet):
            return pad_to_setid(fleet), "fleet"

    latest = query_cloud_latest(pid)
    if latest and _looks_like_version(latest):
        return pad_to_setid(latest), "cloud"

    return DEFAULT_FALLBACK, "fallback"
