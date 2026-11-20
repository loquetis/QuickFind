"""Windows change watcher: one ReadDirectoryChangesW (recursive, synchronous, cancellable) per included drive.
Needs no admin and no extra packages. Events only mark folders/files dirty; the indexer thread does the work."""
import ctypes
import logging
import os
import struct
import threading

import winshell

log = logging.getLogger("quickfind.watcher")

FILE_LIST_DIRECTORY = 0x1
SHARE_ALL = 0x7
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
FILTER = 0x1 | 0x2 | 0x8 | 0x10          # file name, dir name, size, last write
BUF_SIZE = 64 * 1024
ERROR_OPERATION_ABORTED = 995
AVAILABLE = os.name == "nt"


def parse_notifications(buf, nbytes):
    """Decode FILE_NOTIFY_INFORMATION records -> [(action, relative_path)]. Pure function (unit tested)."""
    out, off = [], 0
    while off + 12 <= nbytes:
        nxt, action, ln = struct.unpack_from("<III", buf, off)
        if off + 12 + ln > nbytes:
            break
        name = bytes(buf[off + 12: off + 12 + ln]).decode("utf-16-le", "replace")
        out.append((action, name))
        if not nxt:
            break
        off += nxt
    return out


class Watcher:
    def __init__(self, root, on_event, on_overflow, stop_event):
        self.root, self.on_event, self.on_overflow, self.stop_event = root, on_event, on_overflow, stop_event
        self.state = "starting"
        self.handle = None
        self._closing = False
        self.thread = threading.Thread(target=self._run, name="watch-" + root[:3], daemon=True)

    def start(self):
        if not AVAILABLE:
            self.state = "unavailable"
            return
        self.thread.start()

    def stop(self):
        self._closing = True
        h = self.handle
        if AVAILABLE and h:
            k = ctypes.windll.kernel32
            k.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k.CancelIoEx(h, None)

    def _open(self, k):
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        for path in (self.root, winshell.long_path(self.root)):
            h = k.CreateFileW(path, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING, BACKUP_SEMANTICS, None)
            if h is not None and h != ctypes.c_void_p(-1).value:
                return h
        return None

    def _run(self):
        winshell.set_background_mode()
        k = ctypes.windll.kernel32
        k.ReadDirectoryChangesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                                            ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p, ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        buf = ctypes.create_string_buffer(BUF_SIZE)
        while not self.stop_event.is_set() and not self._closing:
            self.handle = self._open(k)
            if not self.handle:
                self.state = "cannot open"
                self.stop_event.wait(30)
                continue
            self.state = "watching"
            try:
                while not self.stop_event.is_set() and not self._closing:
                    got = ctypes.c_uint(0)
                    ok = k.ReadDirectoryChangesW(self.handle, buf, BUF_SIZE, True, FILTER, ctypes.byref(got), None, None)
                    if not ok:
                        err = ctypes.GetLastError()
                        if err != ERROR_OPERATION_ABORTED:
                            log.warning("ReadDirectoryChangesW failed on %s: %s", self.root, err)
                            self.state = "error %s" % err
                        break
                    if got.value == 0:                      # buffer overflowed: some events were lost
                        self.on_overflow(self.root)
                        continue
                    for action, rel in parse_notifications(buf.raw, got.value):
                        self.on_event(self.root, action, rel)
            finally:
                k.CloseHandle(self.handle)
                self.handle = None
            if not self._closing and not self.stop_event.is_set():
                self.stop_event.wait(10)
        self.state = "stopped"
