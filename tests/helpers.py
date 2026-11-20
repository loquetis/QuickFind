import os, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def make_tree(base):
    def w(rel, data=b"x", mtime=None):
        p = os.path.join(base, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f: f.write(data)
        if mtime: os.utime(p, (mtime, mtime))
    now = time.time()
    w("Documents/Report Final.pdf", b"p" * 5000, now - 86400 * 2)
    w("Documents/report_draft.docx", b"d" * 900, now - 86400 * 40)
    w("Documents/notes.txt", b"n" * 10, now - 3600)
    w("Pictures/holiday/IMG_0001.JPG", b"j" * 3_000_000, now - 86400 * 400)
    w("Pictures/holiday/IMG_0002.jpg", b"j" * 2_000_000, now - 86400 * 3)
    w("Pictures/holiday/readme", b"r")
    w("Music/song.mp3", b"m" * 4_000_000, now - 86400 * 100)
    w("code/app/main.py", b"print(1)"); w("code/app/util.py", b"u")
    w("code/node_modules/skipme/index.js", b"s")
    w("Ünï/çödé ファイル.txt", b"u")
    os.makedirs(os.path.join(base, "empty folder"), exist_ok=True)
    return base

class Env:
    """A temp tree + temp data dir + Database/Settings/Indexer (no thread unless start() is called)."""
    def __init__(self, tree=True, roots=None, **settings):
        import storage, db as dbmod, indexer
        self.tmp = tempfile.mkdtemp(prefix="qf-")
        self.base = os.path.join(self.tmp, "disk"); os.makedirs(self.base)
        self.data = os.path.join(self.tmp, "data"); os.makedirs(self.data)
        if tree: make_tree(self.base)
        self.root = storage.norm_root(self.base)
        self.settings = storage.Settings(os.path.join(self.data, "settings.json"))
        if settings: self.settings.update(settings)
        self.db = dbmod.Database(os.path.join(self.data, "index.db"))
        self.idx = indexer.Indexer(self.db, self.settings, roots_fn=roots or (lambda: [self.root]), data_dir=self.data)
        self.idx._make_rules()
    def scan(self, kind="manual"):
        self.idx._scan_all(kind)
    def names(self, q="", **kw):
        r = self.db.search(dict(q=q, limit=5000, **kw)); return [x["name"] for x in r["results"]]
    def paths(self, q="", **kw):
        r = self.db.search(dict(q=q, limit=5000, **kw)); return [x["folder"] + x["name"] for x in r["results"]]
    def close(self):
        import shutil
        try: self.idx.stop()
        except Exception: pass
        self.db.close(); shutil.rmtree(self.tmp, ignore_errors=True)

class Results:
    def __init__(self): self.r = {}; self.fail = 0
    def ok(self, name, cond, detail=""):
        self.r[name] = bool(cond); self.fail += (not cond)
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)), flush=True)
    def done(self):
        print(f"{len(self.r) - self.fail}/{len(self.r)} passed"); raise SystemExit(1 if self.fail else 0)
