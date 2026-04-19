"""Quick probe: open a TCP connection to LCS as a client and capture
what messages it sends after RegisterClient. Used to verify the proxy
approach is feasible before writing lcs_client.py for real.

Reads the port from SocketPortNumber, opens a socket, sends RegisterClient
(no encryption to start), prints every message LCS sends for 12 seconds,
then disconnects.
"""
import json, os, socket, sys, threading, time
from pathlib import Path

if sys.platform == "win32":
    PORT_FILE = Path(os.environ["PROGRAMDATA"]) / "Poly" / "Lens Control Service" / "SocketPortNumber"
else:
    PORT_FILE = Path.home() / "Library/Application Support/Poly/Lens Control Service/SocketPortNumber"

SOH = "\x01"


def reader(sock):
    buf = ""
    try:
        while True:
            data = sock.recv(8192)
            if not data:
                print("[disconnected]")
                return
            buf += data.decode("utf-8", errors="replace")
            while SOH in buf:
                msg, buf = buf.split(SOH, 1)
                if not msg.strip():
                    continue
                try:
                    obj = json.loads(msg)
                    t = obj.get("type", "?")
                    print(f"<- {t}: {json.dumps(obj)[:300]}")
                except Exception:
                    print(f"<- (non-json): {msg[:200]}")
    except Exception as e:
        print(f"[reader error: {e}]")


def main():
    port = int(PORT_FILE.read_text().strip())
    print(f"Connecting to LCS on 127.0.0.1:{port}")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", port))
    print("Connected.")

    threading.Thread(target=reader, args=(s,), daemon=True).start()

    # Try RegisterClient WITHOUT encryption first - see if LCS accepts it
    reg = {
        "type": "RegisterClient",
        "apiVersion": "1.0.0",
        "name": "polytool-probe",
        "useEncryption": False,
    }
    out = json.dumps(reg) + SOH
    s.sendall(out.encode("utf-8"))
    print(f"-> {reg['type']}")

    # Ask for the device list explicitly
    time.sleep(1)
    q = {"type": "GetDeviceList", "apiVersion": "1.0.0"}
    s.sendall((json.dumps(q) + SOH).encode("utf-8"))
    print(f"-> {q['type']}")

    # Ask for any setting that's likely to be present
    time.sleep(1)
    # Watch for ~12 seconds total
    time.sleep(10)
    s.close()


if __name__ == "__main__":
    main()
