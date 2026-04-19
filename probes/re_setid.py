"""Reverse-engineer SetID protocol from DFUManager.dll without Ghidra.

Strategy:
  1. Locate marker strings ("Performing SetID update.", etc.) in .rdata
  2. For each marker, find its virtual address (ImageBase + offset)
  3. Scan .text for that 4-byte VA - any match is a code xref
  4. Walk backwards from the xref to find the function start (look for
     standard prologues: 55 8B EC = `push ebp; mov ebp, esp` for x86)
  5. Disassemble the function with capstone and dump it for analysis
  6. Identify HID API calls (HidD_SetFeature, HidD_GetFeature, etc.)
     by matching their import addresses

Output: writes one .asm file per SetID method to /tmp/setid_re/.

This won't give us C++ source but it gives us the actual instruction
flow - which is enough to read the HID protocol bytes (we just look
at what gets pushed onto the stack before each HID call).
"""
import os
import re
import sys
from pathlib import Path

import pefile
import capstone


DLL_PATH = r"C:/Program Files/Poly/Poly Studio/LegacyHost/Components/DFUManager/DFUManager.dll"
OUT_DIR = Path("/tmp/setid_re")
OUT_DIR.mkdir(exist_ok=True)

MARKERS = [
    "Performing SetID update.",
    "Failed to enable SetID write mode",
    "Failed to set setID",
    "Failed to disable SetID write mode",
    "Failed getting SetID after update",
    "Failed to get SetID components",
    "SetID update succeeded.",
    "SetID::Start",
    "SetID::get_device",
    "New SetID not correct",
]


def find_string_vas(pe, image_base):
    """Return dict: marker_text -> virtual_address."""
    vas = {}
    rdata_section = next(s for s in pe.sections
                        if s.Name.rstrip(b"\x00") == b".rdata")
    rdata_va = image_base + rdata_section.VirtualAddress
    rdata_bytes = rdata_section.get_data()
    for marker in MARKERS:
        # Try exact + null-terminated
        for needle in (marker.encode() + b"\x00", marker.encode()):
            idx = rdata_bytes.find(needle)
            if idx >= 0:
                vas[marker] = rdata_va + idx
                break
    return vas


def find_xrefs(pe, target_va):
    """Return list of (text_offset, instruction_bytes_around) where
    target_va appears as a 4-byte LE immediate in .text."""
    text_section = next(s for s in pe.sections
                       if s.Name.rstrip(b"\x00") == b".text")
    text_bytes = text_section.get_data()
    text_va = pe.OPTIONAL_HEADER.ImageBase + text_section.VirtualAddress
    needle = target_va.to_bytes(4, "little")
    matches = []
    pos = 0
    while True:
        idx = text_bytes.find(needle, pos)
        if idx < 0:
            break
        matches.append(text_va + idx)
        pos = idx + 1
    return matches


def find_function_start(pe, ref_va, max_back=0x800):
    """Walk backwards from a code address to find the nearest function
    prologue. Common x86 prologues:
        55 8B EC          push ebp; mov ebp, esp
        55 8B EC 83 EC    push ebp; mov ebp, esp; sub esp, X
        56 57 8B          push esi; push edi; mov ...   (less reliable)
    Returns the VA of the function start, or None if not found."""
    text = next(s for s in pe.sections if s.Name.rstrip(b"\x00") == b".text")
    text_va = pe.OPTIONAL_HEADER.ImageBase + text.VirtualAddress
    text_bytes = text.get_data()
    rel = ref_va - text_va
    # Look backwards for `55 8B EC`
    for off in range(rel, max(0, rel - max_back), -1):
        if text_bytes[off:off+3] == b"\x55\x8b\xec":
            return text_va + off
    return None


def disassemble(pe, func_va, max_bytes=0x600):
    """Disassemble starting at func_va. Stops at ret/jmp-tail or after
    max_bytes."""
    text = next(s for s in pe.sections if s.Name.rstrip(b"\x00") == b".text")
    text_va = pe.OPTIONAL_HEADER.ImageBase + text.VirtualAddress
    text_bytes = text.get_data()
    rel = func_va - text_va
    code = text_bytes[rel:rel + max_bytes]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    out = []
    for ins in md.disasm(code, func_va):
        out.append(f"  0x{ins.address:08x}: {ins.mnemonic:6s} {ins.op_str}")
        # Stop at ret (function epilogue)
        if ins.mnemonic in ("ret", "retn"):
            out.append("  --- ret ---")
            break
    return "\n".join(out)


def get_imports(pe):
    """Returns dict {iat_address: 'lib!func'} for known HID-related imports."""
    imports = {}
    if not hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        pe.parse_data_directories()
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        lib = entry.dll.decode("ascii", "replace").lower()
        for imp in entry.imports:
            if imp.name:
                name = imp.name.decode("ascii", "replace")
                imports[imp.address] = f"{lib}!{name}"
    return imports


def main():
    print(f"Loading {DLL_PATH}")
    pe = pefile.PE(DLL_PATH, fast_load=True)
    pe.parse_data_directories()
    image_base = pe.OPTIONAL_HEADER.ImageBase
    print(f"  ImageBase = 0x{image_base:08x}")

    # Find string offsets
    vas = find_string_vas(pe, image_base)
    print(f"\n--- markers found ({len(vas)}/{len(MARKERS)}) ---")
    for marker, va in vas.items():
        print(f"  0x{va:08x}  {marker!r}")

    # Find HID-related imports
    imports = get_imports(pe)
    hid_imports = {k: v for k, v in imports.items()
                   if "hid" in v.lower() or "fwu" in v.lower()}
    print(f"\n--- HID-related imports ({len(hid_imports)}) ---")
    for addr, name in sorted(hid_imports.items())[:20]:
        print(f"  0x{addr:08x}  {name}")

    # For each marker, find xrefs and disassemble surrounding function
    print("\n--- xref-derived function disassembly ---")
    for marker, va in vas.items():
        xrefs = find_xrefs(pe, va)
        if not xrefs:
            print(f"\n[no xrefs] {marker!r}")
            continue
        for xr in xrefs[:1]:  # just the first xref per marker for now
            func = find_function_start(pe, xr)
            if not func:
                print(f"\n[xref but no fn-start] {marker!r} @ 0x{xr:08x}")
                continue
            tag = re.sub(r"[^a-z0-9]+", "_", marker.lower())[:40]
            outpath = OUT_DIR / f"{tag}_0x{func:08x}.asm"
            disasm = disassemble(pe, func)
            outpath.write_text(
                f"# {marker!r}\n"
                f"# xref at 0x{xr:08x}, function start at 0x{func:08x}\n\n"
                + disasm + "\n",
                encoding="utf-8",
            )
            print(f"  [{marker[:35]:<35s}] xref=0x{xr:08x} fn=0x{func:08x}  -> {outpath.name}")

    print(f"\nDone. Output in {OUT_DIR}/")


if __name__ == "__main__":
    main()
