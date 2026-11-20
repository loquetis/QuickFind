"""QuickFind routes. Read-only on the user's files: it only reads folder listings, and opens items it finds
in its own index (by id, never by a path from the browser)."""
import logging
import os
import sqlite3
import threading
import time

import appserver
import db as dbmod
import indexer as indexer_mod
import storage
import watcher
import winshell
from appserver import BadRequest, NotFound

kit = appserver.Kit("QuickFind", "QUICKFIND_PORT", 8771)
HOST, PORT = appserver.HOST, kit.port
last_seen = kit.last_seen
build_server = kit.build_server
VERSION = "1.0.0"
log = logging.getLogger("quickfind.server")
quit_event = threading.Event()
DB = None
SETTINGS = None
IDX = None
EXEC_EXTS = {"url", "scf", "chm", "diagcab", "settingcontent-ms", "library-ms", "jnlp", "ws", "wsc", "psc1", "msc", "docm", "xlsm", "pptm", "dotm", "xlam", "vb", "exe", "com", "bat", "cmd", "msi", "msp", "scr", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta",
             "cpl", "lnk", "jar", "reg", "pif", "appx", "msix", "application", "gadget", "inf"}


def open_database(path):
    """Open the index; a damaged file is moved aside and rebuilt (it is only a cache of folder listings)."""
    try:
        return dbmod.Database(path)
    except sqlite3.DatabaseError:
        log.exception("index database damaged; starting a fresh one")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                try:
                    os.replace(path + suffix, "%s.damaged-%s%s" % (path, stamp, suffix))
                except OSError:
                    try:
                        os.remove(path + suffix)
                    except OSError:
                        pass
        return dbmod.Database(path)


def init(database=None, settings=None, idx=None, roots_fn=None, watcher_cls=None):
    global DB, SETTINGS, IDX
    SETTINGS = settings or storage.Settings()
    DB = database or open_database(os.path.join(storage.DATA_DIR, "index.db"))
    IDX = idx or indexer_mod.Indexer(DB, SETTINGS, roots_fn=roots_fn, watcher_cls=watcher_cls if watcher_cls is not None else (watcher.Watcher if watcher.AVAILABLE else None))


def on_start():
    if IDX is None:
        init()
    winshell.set_process_below_normal()
    IDX.start()


def on_stop():
    if IDX:
        IDX.stop()
    if DB:
        DB.close()


def _drives():
    off = {r.lower() for r in SETTINGS.get()["drives_off"]}
    out = []
    for r in IDX.all_roots():
        out.append({"root": r, "label": winshell.volume_label(r), "enabled": r.lower() not in off,
                    "built": DB.meta_read("built:" + r.lower()) is not None})
    return out


_dirs_cache = [0.0, None]


def _dirs_cached():
    """Folder count for the settings page; counting 2M rows every second would waste CPU, so cache it."""
    if time.time() - _dirs_cache[0] > 20:
        try:
            _dirs_cache[1] = DB.dir_count()
        except Exception:  # noqa: BLE001
            pass
        _dirs_cache[0] = time.time()
    return _dirs_cache[1]


@kit.get("/api/status")
def status(_q):
    prog = IDX.progress()
    dirs = _dirs_cached()
    return {"version": VERSION, "now": time.time(), "settings": SETTINGS.get(), "drives": _drives(), "windows": winshell.IS_WIN,
            "index": dict(prog, dirs_total=dirs, db_bytes=DB.file_bytes(), fts=DB.fts, data_dir=storage.DATA_DIR),
            "watchers": IDX.watcher_states(), "builtin_skip": list(storage.BUILTIN_SKIP), "choices": {"speeds": list(storage.SPEEDS), "rescan": list(storage.RESCAN_CHOICES)},
            "types": list(dbmod.TYPES), "max_limit": dbmod.MAX_LIMIT}


@kit.get("/api/search")
def search(q):
    spec = {k: q.get(k) for k in ("q", "type", "ext", "smin", "smax", "dmin", "dmax", "sort", "dir", "limit")}
    try:
        res = DB.search(spec)
    except dbmod.QueryError as e:
        raise BadRequest(str(e))
    res["now"] = time.time()
    res["entries"] = IDX.entry_count
    return res


def _entry(body):
    try:
        eid = int(body.get("id"))
    except (TypeError, ValueError):
        raise BadRequest("id must be a number")
    info = DB.path_of(eid)
    if not info:
        raise NotFound("That item is no longer in the index")
    return info


def _check_exists(info):
    """Stale index entry? Queue its folder for a re-read and say so."""
    if os.path.lexists(winshell.long_path(info["path"])):
        return True
    with IDX.dlock:
        IDX.dirty_dirs.add(info["folder"])
    with IDX.cv:
        IDX.cv.notify_all()
    return False


@kit.post("/api/open")
def open_item(body):
    info = _entry(body)
    if not _check_exists(info):
        return {"ok": False, "error": "That item no longer exists. The list will refresh."}
    if not info["is_dir"] and info["ext"] in EXEC_EXTS and body.get("confirm_exec") is not True:
        return {"ok": False, "confirm": "exec", "error": "This is a program or script. Confirm to run it."}
    if info["flags"] & dbmod.FLAG_CLOUD and not info["is_dir"] and body.get("allow_download") is not True:
        return {"ok": False, "confirm": "download", "error": "This file is online-only. Opening it downloads it."}
    try:
        if not winshell.open_path(info["path"]):
            return {"ok": False, "error": "Opening files only works on Windows."}
    except OSError as e:
        return {"ok": False, "error": "Windows could not open it: %s" % e}
    return {"ok": True}


@kit.post("/api/reveal")
def reveal_item(body):
    info = _entry(body)
    if not _check_exists(info):
        return {"ok": False, "error": "That item no longer exists. The list will refresh."}
    try:
        if not winshell.reveal(info["path"]):
            return {"ok": False, "error": "Showing in Explorer only works on Windows."}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@kit.post("/api/settings")
def save_settings(body):
    old = SETTINGS.get()
    try:
        new = SETTINGS.update({k: body[k] for k in ("drives_off", "excludes", "skip_system", "skip_hidden", "speed", "rescan_min") if k in body})
    except ValueError as e:
        raise BadRequest(str(e))
    if new != old:
        IDX.apply_settings(old)
    return {"ok": True, "settings": new}


@kit.post("/api/rescan")
def rescan(_b):
    IDX.submit("scan", "manual")
    return {"ok": True}


@kit.post("/api/pause")
def pause(body):
    IDX.pause(bool(body.get("paused")))
    return {"ok": True, "paused": IDX.paused.is_set()}


@kit.post("/api/rebuild")
def rebuild(body):
    if body.get("confirm") is not True:
        raise BadRequest("confirm is required")
    IDX.submit("reset")
    return {"ok": True}


_pick_lock = threading.Lock()


@kit.post("/api/pick-folder")
def pick_folder(_b):
    if not winshell.IS_WIN:
        return {"ok": False, "error": "The folder picker is only available on Windows. Type a path instead."}
    if not _pick_lock.acquire(blocking=False):
        return {"ok": False, "error": "A folder window is already open."}
    try:
        path = winshell.pick_folder()
    finally:
        _pick_lock.release()
    return {"ok": bool(path), "path": path}


@kit.post("/api/quit")
def quit_app(_b):
    quit_event.set()
    return {"ok": True}
