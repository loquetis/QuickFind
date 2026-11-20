"""QuickFind background indexer: walks the included drives (throttled, low priority, no admin), keeps the
index in step with a ReadDirectoryChangesW watcher plus a periodic rescan. One thread owns every write.

Never opens file contents: it only reads directory listings, so OneDrive online-only files are not downloaded."""
import logging
import os
import stat as statmod
import threading
import time

import db as dbmod
import storage
import winshell

log = logging.getLogger("quickfind.indexer")

ATTR_DIR, ATTR_HIDDEN, ATTR_SYSTEM, ATTR_REPARSE = 0x10, 0x2, 0x4, 0x400
ATTR_CLOUD = 0x400000 | 0x40000 | 0x1000        # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE: online-only
RATIO = {"gentle": 2.0, "normal": 0.7, "fast": 0.15}     # sleep per second of work
FLUSH_EVERY = 3.0
MAX_DIRTY = 20000
DRIVE_POLL = 60.0


def is_cloud_tag(tag):
    """OneDrive / cloud-files placeholder reparse tags: these folders are real, we do list them."""
    return (tag & 0xFFFF0FFF) == 0x9000001A or tag == 0x80000021


def sanitize(name):
    return dbmod.clean_text(name)


def entry_info(st):
    """(is_dir, flags, descend) from an lstat-style result. Windows uses file attributes; elsewhere symlinks are links."""
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:
        is_dir = bool(attrs & ATTR_DIR)
        flags = (dbmod.FLAG_HIDDEN if attrs & ATTR_HIDDEN else 0) | (dbmod.FLAG_SYSTEM if attrs & ATTR_SYSTEM else 0)
        link = False
        if attrs & ATTR_REPARSE:
            tag = getattr(st, "st_reparse_tag", 0)
            if is_dir and not is_cloud_tag(tag):
                link = True
        if attrs & ATTR_CLOUD and not is_dir:
            flags |= dbmod.FLAG_CLOUD
        if link:
            flags |= dbmod.FLAG_LINK
        return is_dir, flags, is_dir and not link
    if statmod.S_ISLNK(st.st_mode):
        return False, dbmod.FLAG_LINK, False
    is_dir = statmod.S_ISDIR(st.st_mode)
    return is_dir, 0, is_dir


class Rules:
    """Snapshot of the settings the walker needs."""

    def __init__(self, roots, settings, own_dir):
        self.roots = roots
        self.prefixes = []
        for p in list(settings.get("excludes", [])) + [own_dir]:
            try:
                p = storage.norm_folder(p)
            except ValueError:
                continue
            self.prefixes.append((p if p.endswith(os.sep) else p + os.sep).lower())
        self.prefixes = tuple(self.prefixes)
        self.skip_system = settings.get("skip_system", True)
        self.skip_hidden = settings.get("skip_hidden", False)
        self.ratio = RATIO.get(settings.get("speed"), RATIO["gentle"])
        self.builtin = tuple(n.lower() for n in storage.BUILTIN_SKIP)
        self.marks = tuple(os.sep + n + os.sep for n in self.builtin)

    def excluded(self, dir_path):
        """dir_path ends with the separator. True inside an excluded folder or a built-in skipped one."""
        lc = dir_path.lower()
        if self.prefixes and lc.startswith(self.prefixes):
            return True
        return any(m in lc for m in self.marks)

    def root_of(self, path):
        lc = path.lower()
        for r in self.roots:
            if lc.startswith(r.lower()):
                return r
        return None


class Throttle:
    """Keeps the duty cycle low: after ~20 ms of work, sleep ratio x the time worked. Honors pause and stop."""

    def __init__(self, stop, paused):
        self.stop, self.paused = stop, paused
        self.t0 = time.monotonic()

    def reset(self):
        self.t0 = time.monotonic()

    def tick(self, ratio):
        while self.paused.is_set() and not self.stop.is_set():
            self.stop.wait(0.4)
            self.t0 = time.monotonic()
        busy = time.monotonic() - self.t0
        if busy >= 0.02:
            self.stop.wait(min(busy * ratio, 0.6))
            self.t0 = time.monotonic()


class Indexer:
    def __init__(self, database, settings, roots_fn=None, watcher_cls=None, data_dir=None):
        self.db = database
        self.settings = settings
        self.roots_fn = roots_fn or self._default_roots
        self.watcher_cls = watcher_cls
        self.data_dir = data_dir or storage.DATA_DIR
        self.stop_ev = threading.Event()
        self.paused = threading.Event()
        self.cv = threading.Condition()
        self.jobs = []
        self.dirty_dirs, self.dirty_files = set(), set()
        self.dlock = threading.Lock()
        self.overflow = set()
        self.thread = None
        self.watchers = {}
        self.rules = None
        self.throttle = Throttle(self.stop_ev, self.paused)
        self.last_flush = time.monotonic()
        self.next_scan = None
        self.last_drives = None
        self.last_drive_poll = 0.0
        self.state = {"phase": "starting", "kind": None, "root": None, "current": None, "dirs": 0, "seen": 0, "expected": None,
                      "root_index": 0, "root_total": 0, "started": None, "last_done": None, "errors": 0, "events": 0, "watch_overflows": 0}
        self.entry_count = 0

    # ------------------------------------------------------------- roots / rules
    def _default_roots(self):
        env = os.environ.get("QUICKFIND_ROOTS")
        if env:
            out = []
            for p in env.split(os.pathsep):
                if p.strip() and os.path.isdir(p.strip()):
                    out.append(storage.norm_root(p))
            return out
        return winshell.fixed_drives()

    def all_roots(self):
        return list(self.roots_fn())

    def enabled_roots(self):
        off = {r.lower() for r in self.settings.get()["drives_off"]}
        return [r for r in self.all_roots() if r.lower() not in off]

    def _make_rules(self):
        self.rules = Rules(self.enabled_roots(), self.settings.get(), self.data_dir)
        return self.rules

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._make_rules()
        self.thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        with self.cv:
            self.cv.notify_all()
        for w in list(self.watchers.values()):
            w.stop()
        if self.thread:
            self.thread.join(timeout=5)

    def submit(self, *job):
        with self.cv:
            if job not in self.jobs:
                self.jobs.append(job)
            self.cv.notify_all()

    def pause(self, flag):
        (self.paused.set if flag else self.paused.clear)()

    def apply_settings(self, old):
        """Called after the settings changed. Queues the purge/scan work on the indexer thread."""
        self.submit("apply", old)

    # ------------------------------------------------------------- watcher events (watcher threads)
    def on_event(self, root, action, rel):
        if self.stop_ev.is_set():
            return
        rules = self.rules
        path = os.path.join(root, rel)
        parent = os.path.dirname(path)
        parent = parent if parent.endswith(os.sep) else parent + os.sep
        if rules.excluded(parent) or rules.excluded(path + os.sep):
            return
        self.state["events"] += 1
        with self.dlock:
            if action == 3:       # modified: cheap single-file refresh
                if len(self.dirty_files) < MAX_DIRTY:
                    self.dirty_files.add(path)
                else:
                    self.overflow.add(root)       # too many to track: rescan the drive instead
            else:
                if len(self.dirty_dirs) < MAX_DIRTY:
                    self.dirty_dirs.add(parent)
                else:
                    self.overflow.add(root)
        with self.cv:
            self.cv.notify_all()

    def on_overflow(self, root):
        self.state["watch_overflows"] += 1
        with self.dlock:
            self.overflow.add(root)
        with self.cv:
            self.cv.notify_all()

    def _sync_watchers(self):
        wanted = set(self.rules.roots) if self.watcher_cls else set()
        for r in list(self.watchers):
            if r not in wanted:
                self.watchers.pop(r).stop()
        for r in wanted:
            if r not in self.watchers:
                w = self.watcher_cls(r, self.on_event, self.on_overflow, self.stop_ev)
                self.watchers[r] = w
                w.start()

    def watcher_states(self):
        return [{"root": r, "state": w.state} for r, w in self.watchers.items()]

    # ------------------------------------------------------------- main loop
    def _run(self):
        winshell.set_background_mode()
        try:
            self.entry_count = self.db.counts()[0]
        except Exception:  # noqa: BLE001
            log.exception("count failed")
        self.last_drives = tuple(self.all_roots())
        self._sync_watchers()
        self.submit("scan", "startup")
        while not self.stop_ev.is_set():
            try:
                job = None
                with self.cv:
                    if not self.jobs:
                        self.cv.wait(1.0)
                    if self.jobs:
                        job = self.jobs.pop(0)
                if job:
                    self._do(job)
                self._housekeeping()
            except Exception:  # noqa: BLE001
                log.exception("indexer loop error")
                self.state["errors"] += 1
                if self.state["phase"] != "idle":
                    self._set_idle()
                try:
                    self.db.commit()
                except Exception:  # noqa: BLE001
                    pass
                self.stop_ev.wait(2.0)
        try:
            self.db.commit()
        except Exception:  # noqa: BLE001
            pass

    def _housekeeping(self):
        now = time.monotonic()
        if (self.dirty_dirs or self.dirty_files or self.overflow) and now - self.last_flush >= FLUSH_EVERY and not self.paused.is_set():
            self._flush_dirty()
            self.last_flush = time.monotonic()
        iv = self.settings.get()["rescan_min"]
        if iv and self.state["phase"] == "idle" and self.next_scan and time.time() >= self.next_scan and not self.jobs:
            self.submit("scan", "periodic")
        if now - self.last_drive_poll >= DRIVE_POLL:
            self.last_drive_poll = now
            cur = tuple(self.all_roots())
            if cur != self.last_drives:
                self.last_drives = cur
                self.apply_settings(self.settings.get())
        if self.state["phase"] == "idle":
            self.db.maybe_commit(force=True)

    def _do(self, job):
        kind = job[0]
        self.throttle.reset()
        if kind == "scan":
            self._scan_all(job[1])
        elif kind == "apply":
            self._apply(job[1])
        elif kind == "tree":
            self._scan_tree(job[1], "tree")
            self.db.commit()
        elif kind == "reset":
            self._reset()

    # ------------------------------------------------------------- jobs
    def _set_idle(self, keep_next=False):
        self.state.update(phase="idle", current=None, root=None, kind=None)
        if not keep_next:
            iv = self.settings.get()["rescan_min"]
            self.next_scan = time.time() + iv * 60 if iv else None

    def _scan_all(self, kind):
        rules = self._make_rules()
        self._sync_watchers()
        roots = rules.roots
        self.state.update(phase="scanning", kind=kind, root_total=len(roots), started=time.time(), dirs=0, seen=0, errors=0)
        for i, root in enumerate(roots):
            if self.stop_ev.is_set():
                break
            self.state.update(root=root, root_index=i + 1)
            ok = self._scan_root(root, kind)
            if ok:
                self.db.meta_set("built:" + root.lower(), int(time.time()))
        self.db.commit()
        self.db.checkpoint()
        self.entry_count = self.db.counts()[0]
        self.state["last_done"] = time.time()
        self._set_idle()

    def _scan_root(self, root, kind):
        exp = self.db.meta_get("count:" + root.lower())
        self.state.update(expected=int(exp) if exp else None, dirs=0, seen=0)
        complete = self._scan_tree(root, kind)
        if complete:
            self.db.meta_set("count:" + root.lower(), self.state["seen"])
        return complete

    def _scan_tree(self, start, kind):
        """Depth-first walk of one folder tree, diffing each listing against the index. False if cancelled/unreadable."""
        rules = self.rules
        start = start if start.endswith(os.sep) else start + os.sep
        stack = [start]
        first = True
        while stack:
            if self.stop_ev.is_set():
                self.db.commit()
                return False
            path = stack.pop()
            if rules.excluded(path):
                continue
            self.state["current"] = path
            subdirs, ok = self.sync_dir(path, new_only=False)
            if first and not ok:
                return False
            first = False
            stack.extend(subdirs)
            self.state["dirs"] += 1
            self.db.maybe_commit()
            self.throttle.tick(rules.ratio)
        return True

    def sync_dir(self, path, new_only):
        """Make the index of one folder match the disk. Returns (subfolders to walk next, listing_ok)."""
        rules = self.rules
        listing, complete = self._list(path, rules)
        if listing is None:
            # folder is gone -> drop it from the index; unreadable (permissions) -> keep what we have
            if not os.path.isdir(winshell.long_path(path)) and not os.path.ismount(path):
                if rules.root_of(path) and path.lower() not in [r.lower() for r in rules.roots]:
                    self.db.purge_prefix(path)
            return [], False
        did = self.db.dir_id(path)
        existing = self.db.children(did)
        add, upd, gone, subdirs = [], [], [], []
        seen = set()
        for name, is_dir, size, mtime, flags, descend in listing:
            seen.add(name)
            old = existing.get(name)
            if old is None:
                add.append((name, is_dir, size, mtime, flags))
                if descend:
                    subdirs.append(path + name + os.sep)
                continue
            oid, osize, omtime, ois_dir, oflags = old
            if bool(ois_dir) != is_dir:
                gone.append((oid, name, bool(ois_dir)))
                add.append((name, is_dir, size, mtime, flags))
                if descend:
                    subdirs.append(path + name + os.sep)
                continue
            if (osize, omtime, oflags) != (size, mtime, flags):
                upd.append((size, mtime, flags, oid))
            if descend and not new_only:
                subdirs.append(path + name + os.sep)
        if complete:
            for name, row in existing.items():
                if name not in seen:
                    gone.append((row[0], name, bool(row[3])))
        for oid, name, was_dir in gone:
            if was_dir:
                self.db.purge_prefix(path + name + os.sep)
        if gone:
            self.db.delete_entries([g[0] for g in gone])
        if add:
            self.db.add_entries(did, add)
        if upd:
            self.db.update_entries(upd)
        self.entry_count += len(add) - len(gone)
        self.state["seen"] += len(listing)
        return subdirs, True

    def _list(self, path, rules):
        """[(name, is_dir, size, mtime, flags, descend)], complete?  (None, False) when the folder cannot be listed."""
        out = []
        try:
            it = os.scandir(winshell.long_path(path))
        except OSError:
            self.state["errors"] += 1
            return None, False
        complete = True
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    complete = False
                    self.state["errors"] += 1
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                    name = sanitize(e.name)
                    is_dir, flags, descend = entry_info(st)
                except (OSError, ValueError):
                    continue
                if rules.skip_system and flags & dbmod.FLAG_SYSTEM:
                    continue
                if rules.skip_hidden and flags & dbmod.FLAG_HIDDEN:
                    continue
                if is_dir:
                    lc = name.lower()
                    if lc in rules.builtin or rules.excluded(path + name + os.sep):
                        continue
                    size = None
                else:
                    size = st.st_size
                try:
                    mtime = int(st.st_mtime)
                except (OverflowError, OSError, ValueError):
                    mtime = None
                out.append((name, is_dir, size, mtime, flags, descend))
        return out, complete

    def _flush_dirty(self):
        rules = self._make_rules()
        with self.dlock:
            dirs, files, over = self.dirty_dirs, self.dirty_files, self.overflow
            self.dirty_dirs, self.dirty_files, self.overflow = set(), set(), set()
        self.state["phase"] = "updating"
        try:
            for root in over:
                if root in rules.roots:
                    self._scan_root(root, "overflow")
            for path in sorted(dirs):
                if self.stop_ev.is_set():
                    break
                if not rules.root_of(path) or rules.excluded(path):
                    continue
                if self.db.dir_id(path, create=False) is None and path.lower() not in [r.lower() for r in rules.roots]:
                    continue          # not indexed (skipped as system/hidden/link, or the scan has not reached it yet)
                self._sync_event_dir(path)
                self.throttle.tick(rules.ratio)
            for f in files:
                if self.stop_ev.is_set():
                    break
                self._touch(f, rules)
                self.throttle.tick(rules.ratio)
        finally:
            self.db.commit()
            if self.state["phase"] == "updating":
                self._set_idle(keep_next=True)

    def _sync_event_dir(self, path):
        stack = [(path, True)]
        while stack:
            p, new_only = stack.pop()
            if self.stop_ev.is_set():
                return
            subdirs, _ = self.sync_dir(p, new_only=new_only)
            stack.extend((s, False) for s in subdirs)

    def _touch(self, path, rules):
        parent = os.path.dirname(path)
        parent = parent if parent.endswith(os.sep) else parent + os.sep
        name = sanitize(os.path.basename(path))
        row = self.db.find_entry(parent, name)
        if row is None:
            if self.db.dir_id(parent, create=False) is not None:
                self._sync_event_dir(parent)
            return
        try:
            st = os.lstat(winshell.long_path(path))
            is_dir, flags, _ = entry_info(st)
        except OSError:
            self._sync_event_dir(parent)
            return
        size = None if is_dir else st.st_size
        try:
            mtime = int(st.st_mtime)
        except (OverflowError, OSError, ValueError):
            mtime = None
        if (row[1], row[2], row[4]) != (size, mtime, flags) and bool(row[3]) == is_dir:
            self.db.update_entries([(size, mtime, flags, row[0])])

    def _apply(self, old):
        """Settings or drives changed: purge what is no longer wanted, scan what is newly wanted."""
        new = self.settings.get()
        rules = self._make_rules()
        self._sync_watchers()
        keep = {r.lower() for r in rules.roots}
        self.state.update(phase="updating", kind="settings")
        off = {r.lower() for r in new["drives_off"]}
        for r in self._indexed_roots():                 # drive turned off (an unplugged drive stays searchable)
            if r.lower() in off and r.lower() not in keep:
                self.db.purge_prefix(r)
                self.db.meta_del_prefix("built:" + r.lower())
                self.db.meta_del_prefix("count:" + r.lower())
        for ex in new["excludes"]:                      # folders now excluded
            parent, name = os.path.split(ex.rstrip("\\/"))
            if not rules.root_of(ex + os.sep):
                continue
            self.db.purge_prefix(ex + os.sep)
            parent = parent if parent.endswith(os.sep) else parent + os.sep
            row = self.db.find_entry(parent, sanitize(name))
            if row:
                self.db.delete_entries([row[0]])
        self.db.commit()
        if old.get("skip_system") != new["skip_system"] or old.get("skip_hidden") != new["skip_hidden"] or \
                any(self.db.meta_get("built:" + r.lower()) is None for r in rules.roots):
            self._scan_all("settings")
            return
        keep_ex = {x.lower() for x in new["excludes"]}
        for ex in old.get("excludes", []):              # folders no longer excluded: the parent listing adds them back
            if ex.lower() in keep_ex or not rules.root_of(ex + os.sep):
                continue
            parent = os.path.dirname(ex.rstrip("\\/"))
            parent = parent if parent.endswith(os.sep) else parent + os.sep
            self._sync_event_dir(parent)
        self.db.commit()
        self.entry_count = self.db.counts()[0]
        self._set_idle(keep_next=True)

    def _indexed_roots(self):
        """Top-level roots present in the index (the shortest path of each drive/tree)."""
        rows = [r[0] for r in self.db.w.execute("SELECT path FROM dirs ORDER BY path_lc")]
        roots, last = [], None
        for p in rows:
            if last is None or not p.lower().startswith(last.lower()):
                roots.append(p)
                last = p
        return roots

    def _reset(self):
        self.state["phase"] = "resetting"
        self.db.reset()
        self.entry_count = 0
        self._scan_all("rebuild")

    # ------------------------------------------------------------- status
    def progress(self):
        s = dict(self.state)
        if self.paused.is_set():
            s["phase"] = "paused"
        exp, seen = s.get("expected"), s.get("seen", 0)
        s["pct"] = min(99, int(seen * 100 / exp)) if exp and self.state["phase"] == "scanning" else None
        s["entries"] = self.entry_count
        s["next_scan"] = self.next_scan
        s["paused"] = self.paused.is_set()
        return s


def _safe(p):
    try:
        storage.norm_folder(p)
        return True
    except ValueError:
        return False
