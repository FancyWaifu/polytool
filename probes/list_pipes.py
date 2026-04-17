import ctypes
import ctypes.wintypes as wt


class WFD(ctypes.Structure):
    _fields_ = [
        ('dwFileAttributes', wt.DWORD), ('ftCreationTime', wt.FILETIME),
        ('ftLastAccessTime', wt.FILETIME), ('ftLastWriteTime', wt.FILETIME),
        ('nFileSizeHigh', wt.DWORD), ('nFileSizeLow', wt.DWORD),
        ('dwReserved0', wt.DWORD), ('dwReserved1', wt.DWORD),
        ('cFileName', wt.WCHAR * 260), ('cAlternateFileName', wt.WCHAR * 14),
    ]


k = ctypes.WinDLL('kernel32', use_last_error=True)
k.FindFirstFileW.argtypes = [wt.LPCWSTR, ctypes.POINTER(WFD)]
k.FindFirstFileW.restype = wt.HANDLE
k.FindNextFileW.argtypes = [wt.HANDLE, ctypes.POINTER(WFD)]
k.FindNextFileW.restype = wt.BOOL
k.FindClose.argtypes = [wt.HANDLE]
k.FindClose.restype = wt.BOOL

INVALID = ctypes.c_void_p(-1).value

d = WFD()
h = k.FindFirstFileW(r"\\.\pipe\*", ctypes.byref(d))
if h == INVALID:
    print("FindFirstFile failed:", ctypes.get_last_error())
else:
    pipes = []
    while True:
        name = d.cFileName
        if any(s in name.lower() for s in ("poly", "lens", "legacy", "dfu", "plt", "host")):
            pipes.append(name)
        if not k.FindNextFileW(h, ctypes.byref(d)):
            break
    k.FindClose(h)
    print(f"Found {len(pipes)} matching pipes:")
    for p in pipes:
        print(f"  \\\\.\\pipe\\{p}")
