#!/usr/bin/env python3
"""Hook CreateProcessW in LensService.exe to capture spawned process command lines.

Especially useful for capturing how LCS spawns LegacyHost.exe and LegacyDfu.exe
during DFU operations.
"""
import frida
import sys
import time

JS = r"""
const cp = Process.getModuleByName('kernel32.dll').findExportByName( 'CreateProcessW');
if (!cp) {
    console.log('[!] CreateProcessW not found');
} else {
    Interceptor.attach(cp, {
        onEnter: function (args) {
            // CreateProcessW(LPCWSTR appName, LPWSTR cmdLine, ...)
            const appName = args[0].isNull() ? '' : args[0].readUtf16String();
            const cmdLine = args[1].isNull() ? '' : args[1].readUtf16String();
            send({ type: 'create_process', appName: appName, cmdLine: cmdLine });
        }
    });
    console.log('[+] Hooked CreateProcessW');
}

// Also hook the A variant
const cpa = Process.getModuleByName('kernel32.dll').findExportByName( 'CreateProcessA');
if (cpa) {
    Interceptor.attach(cpa, {
        onEnter: function (args) {
            const appName = args[0].isNull() ? '' : args[0].readCString();
            const cmdLine = args[1].isNull() ? '' : args[1].readCString();
            send({ type: 'create_process', appName: appName, cmdLine: cmdLine });
        }
    });
    console.log('[+] Hooked CreateProcessA');
}

// Also hook CreateNamedPipeA/W to see pipe creation
['CreateNamedPipeA', 'CreateNamedPipeW'].forEach(fn => {
    const a = Process.getModuleByName('kernel32.dll').findExportByName( fn);
    if (a) {
        const isW = fn.endsWith('W');
        Interceptor.attach(a, {
            onEnter: function (args) {
                const name = isW ? args[0].readUtf16String() : args[0].readCString();
                if (name && (name.toLowerCase().indexOf('legacy') >= 0 || name.toLowerCase().indexOf('dfu') >= 0 || name.toLowerCase().indexOf('clockwork') >= 0)) {
                    send({ type: 'create_pipe', name: name });
                }
            }
        });
        console.log('[+] Hooked ' + fn);
    }
});
"""


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "LensService.exe"
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == target]
    if not procs:
        sys.exit(f"{target} not running")
    pid = procs[0].pid
    print(f"Attaching to {target} (pid {pid})")

    session = mgr.attach(pid)
    script = session.create_script(JS)

    def on_message(msg, data):
        if msg.get("type") == "send":
            p = msg.get("payload", {})
            t = p.get("type", "?")
            if t == "create_process":
                print(f"[CreateProcess] app={p.get('appName')!r}")
                print(f"                cmd={p.get('cmdLine')!r}")
            elif t == "create_pipe":
                print(f"[CreatePipe] {p.get('name')}")
        else:
            print(f"[meta] {msg}")

    script.on("message", on_message)
    script.load()
    print("Hook installed. Trigger an operation in Poly Studio. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        session.detach()


if __name__ == "__main__":
    main()
