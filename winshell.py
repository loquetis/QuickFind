"""Windows-only helpers via ctypes: fixed drives, volume labels, folder picker, open/reveal in Explorer.
No elevation, no PowerShell. On other systems (the box test environment) they return empty values."""
import os
import subprocess

IS_WIN = os.name == "nt"


def long_path(p):
    """Prefix with \\\\?\\ on Windows so paths over 260 chars work."""
    if not IS_WIN:
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def fixed_drives():
    if not IS_WIN:
        return []
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
                out.append(root)
    return out


def volume_label(root):
    if not IS_WIN:
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
    except OSError:
        return ""
    return buf.value if ok else ""


def set_background_mode():
    """Lower this thread's CPU and disk priority (THREAD_MODE_BACKGROUND_BEGIN). Windows only."""
    if not IS_WIN:
        return False
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentThread.restype = ctypes.c_void_p
    k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return bool(k.SetThreadPriority(k.GetCurrentThread(), 0x00010000))


def set_process_below_normal():
    if not IS_WIN:
        return False
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    return bool(k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000))


def open_path(path):
    """Open a file with its default app, or a folder in Explorer (same as double-clicking it)."""
    if not IS_WIN:
        return False
    os.startfile(path)  # noqa: S606 - path comes from the index, checked by the server
    return True


def reveal(path):
    """Open the containing folder with the item selected."""
    if not IS_WIN:
        return False
    p = os.path.abspath(path)
    subprocess.Popen(["explorer.exe", "/select," + p], close_fds=True)
    return True


def pick_folder(title="Choose a folder to skip"):
    """Native folder dialog without tkinter. Returns a path or None."""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p), ("pszDisplayName", wintypes.LPWSTR),
                    ("lpszTitle", wintypes.LPCWSTR), ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize(None)
    try:
        buf = ctypes.create_unicode_buffer(1024)
        bi = BROWSEINFOW()
        bi.pszDisplayName = ctypes.cast(buf, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = 0x1 | 0x40 | 0x10     # RETURNONLYFSDIRS | NEWDIALOGSTYLE | EDITBOX
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        out = ctypes.create_unicode_buffer(1024)
        ok = shell32.SHGetPathFromIDListW(pidl, out)
        ole32.CoTaskMemFree(pidl)
        return out.value if ok else None
    finally:
        ole32.OleUninitialize()
