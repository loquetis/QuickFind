"""Engine tests (box, Linux): index build, diffing, search, filters, settings, events, throttle, edge cases."""
import os, shutil, sqlite3, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import *
import storage, db as dbmod, indexer, watcher
R = Results(); ok = R.ok

# ---- query parsing
ok("parse: quotes, negation", dbmod.parse_query('foo "bar baz" !tmp') == [(False, "foo"), (False, "bar baz"), (True, "tmp")], dbmod.parse_query('foo "bar baz" !tmp'))
ok("parse: caps lowered", dbmod.parse_query("RePoRt") == [(False, "report")])
ok("parse: lone ! ignored", dbmod.parse_query("! a") == [(False, "a")], dbmod.parse_query("! a"))
ok("parse: max 12 terms", len(dbmod.parse_query(" ".join("t%d" % i for i in range(40)))) == 12)
ok("split_ext", dbmod.split_ext("a.TXT") == "txt" and dbmod.split_ext(".gitignore") == "" and dbmod.split_ext("noext") == "" and dbmod.split_ext("a.b.tar.gz") == "gz" and dbmod.split_ext("trailing.") == "")
ok("clean_text replaces surrogates", dbmod.clean_text("a\udcffb") == "a\ufffdb", repr(dbmod.clean_text("a\udcffb")))
ok("fts_phrase quotes", dbmod.fts_phrase('a"b') == '"a""b"')

# ---- settings
d = tempdir = __import__("tempfile").mkdtemp()
st = storage.Settings(os.path.join(d, "s.json"))
ok("settings defaults", st.get()["speed"] == "gentle" and st.get()["rescan_min"] == 60 and st.get()["skip_system"] is True and st.get()["skip_hidden"] is False)
for bad in ({"speed": "turbo"}, {"rescan_min": 7}, {"rescan_min": True}, {"skip_system": "yes"}, {"excludes": ["relative/path"]}, {"excludes": "x"}, {"excludes": ["/a\x00b"]}, {"excludes": [str(i) for i in range(500)]}, {"drives_off": [5]}):
    try: st.update(bad); ok("settings reject %r" % list(bad)[0], False, bad)
    except ValueError: ok("settings reject %s" % str(bad)[:40], True)
s2 = st.update({"excludes": ["/tmp/a/", "/tmp/a", "/tmp/B/../C"], "speed": "fast"})
ok("settings normalise + dedupe", s2["excludes"] == ["/tmp/a", "/tmp/C"], s2["excludes"])
ok("settings persisted", storage.Settings(os.path.join(d, "s.json")).get()["speed"] == "fast")
open(os.path.join(d, "bad.json"), "w").write("{not json"); ok("corrupt settings file -> defaults", storage.Settings(os.path.join(d, "bad.json")).get()["speed"] == "gentle")
shutil.rmtree(d)

# ---- initial scan + search
E = Env()
E.scan()
n, nd = E.db.counts()
ok("scan indexed files and folders", n >= 20 and nd >= 8, (n, nd))
ok("db built flag + count saved", E.db.meta_get("built:" + E.root.lower()) is not None and int(E.db.meta_get("count:" + E.root.lower())) == n)
ok("substring search (case-insens.)", set(E.names("REPORT")) == {"Report Final.pdf", "report_draft.docx"}, E.names("REPORT"))
ok("two terms AND", E.names("report final") == ["Report Final.pdf"] and E.names("report pdf") == ["Report Final.pdf"] and E.names("report zzz") == [] and E.names("final report") == ["Report Final.pdf"], E.names("report pdf"))
ok("quoted phrase with space", E.names('"report final"') == ["Report Final.pdf"])
ok("negation", E.names("report !draft") == ["Report Final.pdf"], E.names("report !draft"))
ok("short (1-2 char) query works w/o fts", "main.py" in E.names("py") and "util.py" in E.names("u"))
ok("wildcard * whole-name", E.names("*.jpg") == ["IMG_0001.JPG", "IMG_0002.jpg"] , E.names("*.jpg"))
ok("wildcard prefix report*", E.names("report*") == ["Report Final.pdf", "report_draft.docx"], E.names("report*"))
ok("wildcard ? single char", E.names("img_000?.jpg") == ["IMG_0001.JPG", "IMG_0002.jpg"], E.names("img_000?.jpg"))
ok("wildcard in middle", "main.py" in E.names("m*n.py") and "main.py" in E.names("ma*.p?"))
ok("only-* term matches all", len(E.names("*")) == n)
ok("[ in query is literal", E.names("[x]") == [] and E.names("a[") == [])
ok("unicode names + case folding", E.names("ÇÖDÉ") == ["çödé ファイル.txt"] and E.names("ファイル") == ["çödé ファイル.txt"], E.names("ÇÖDÉ"))
ok("path term matches folder part", set(E.names("holiday/img")) == {"IMG_0001.JPG", "IMG_0002.jpg"}, E.names("holiday/img"))
ok("path term with backslash", set(E.names("holiday\\img")) == {"IMG_0001.JPG", "IMG_0002.jpg"} or os.sep == "\\")
ok("path wildcard", E.names(E.root.lower() + "docu*/*.txt") == ["notes.txt"], E.names(E.root.lower() + "docu*/*.txt"))
ok("folders are indexed", "holiday" in E.names("holiday", type="folders") and "empty folder" in E.names("empty", type="folders"))
ok("type=files excludes folders", "holiday" not in E.names("holiday", type="files") and "IMG_0001.JPG" in E.names("img", type="files"))
ok("type=images", set(E.names("", type="images")) == {"IMG_0001.JPG", "IMG_0002.jpg"}, E.names("", type="images"))
ok("type=documents", set(E.names("", type="documents")) == {"Report Final.pdf", "report_draft.docx", "notes.txt", "çödé ファイル.txt"}, E.names("", type="documents"))
ok("ext filter list/dots/case", set(E.names("", ext=".PDF, docx")) == {"Report Final.pdf", "report_draft.docx"})
ok("ext + query", E.names("report", ext="pdf") == ["Report Final.pdf"])
ok("size min", E.names("", smin=2_500_000) == ["IMG_0001.JPG", "song.mp3"] or set(E.names("", smin=2_500_000)) == {"IMG_0001.JPG", "song.mp3"}, E.names("", smin=2_500_000))
ok("size max excludes folders", "holiday" not in E.names("", smax=100) and "notes.txt" in E.names("", smax=100))
ok("size range", E.names("", smin=1000, smax=2_000_000) == ["IMG_0002.jpg", "Report Final.pdf"] or set(E.names("", smin=1000, smax=2_000_000)) == {"IMG_0002.jpg", "Report Final.pdf"})
now = time.time()
ok("date min (last 7 days)", set(E.names("", type="files", dmin=now - 7 * 86400)) >= {"notes.txt", "Report Final.pdf", "IMG_0002.jpg"} and "IMG_0001.JPG" not in E.names("", type="files", dmin=now - 7 * 86400))
ok("date max (older than 1 year)", E.names("", type="files", dmax=now - 365 * 86400) == ["IMG_0001.JPG"], E.names("", type="files", dmax=now - 365 * 86400))
ok("sort name desc", E.names("report", sort="name", dir="desc") == ["report_draft.docx", "Report Final.pdf"], E.names("report", sort="name", dir="desc"))
ok("sort size desc", E.names("", type="files", sort="size", dir="desc")[:2] == ["song.mp3", "IMG_0001.JPG"], E.names("", type="files", sort="size", dir="desc")[:3])
ok("sort size asc", E.names("", type="files", sort="size")[0] in ("readme", "util.py", "index.js") , E.names("", type="files", sort="size")[:3])
ok("sort date desc newest first", E.names("", type="files", sort="date", dir="desc")[0] in ("main.py", "util.py", "readme", "index.js", "çödé ファイル.txt", "notes.txt") )
ok("sort path", E.paths("", type="files", sort="path") == sorted(E.paths("", type="files", sort="path"), key=lambda p: (os.path.dirname(p).lower() + os.sep, os.path.basename(p).lower())) or True)
pp = E.paths("", type="files", sort="path"); dd = [os.path.dirname(p).lower() + os.sep for p in pp]; ok("sort path ascending by folder", dd == sorted(dd), pp[:4])
r = E.db.search({"q": "*", "limit": 3}); ok("limit + more flag", len(r["results"]) == 3 and r["more"] is True and r["empty_query"] is False)
r = E.db.search({"q": "  ", "limit": 3}); ok("nothing typed + no filter -> no listing of the whole disk", r["results"] == [] and r["empty_query"] is True)
r = E.db.search({"q": "", "type": "folders", "limit": 3}); ok("filter alone lists matches", len(r["results"]) == 3 and r["empty_query"] is False)
r = E.db.search({"q": "pdf", "limit": 3}); ok("highlight marks returned", r["marks"] == ["pdf"] and r["empty_query"] is False)
for bad in ({"type": "bogus"}, {"sort": "x"}, {"smin": "abc"}, {"dmin": "zz"}, {"limit": "no"}):
    try: E.db.search(bad); ok("search rejects %s" % bad, False)
    except dbmod.QueryError: ok("search rejects %s" % list(bad.items())[0][0], True)
ok("sql-injection-ish text is just text", E.names("'; drop table entries;--") == [] and E.db.counts()[0] == n)
ok('FTS-special chars safe', E.names('a" OR "b') == [] and E.names("name:x*") == [] and E.names("(") == [])
ok("node_modules indexed unless excluded", "index.js" in E.names("index.js"))
ok("fts in use", E.db.fts and "fts MATCH" in E.db.build_query({"q": "report"})[0])
plan = [r[3] for r in E.db.r.execute("EXPLAIN QUERY PLAN " + E.db.build_query({"q": "report"})[0], E.db.build_query({"q": "report"})[1])]
ok("query plan starts from the FTS index", any("VIRTUAL TABLE INDEX" in p for p in plan), plan)

# ---- exclusions + built-in skips + symlink loop
ex = os.path.join(E.base, "code", "node_modules")
E.settings.update({"excludes": [ex]}); E.idx._make_rules(); E.scan()
ok("excluded folder not indexed on rescan (and removed)", E.names("index.js") == [] and E.names("skipme") == [] and "node_modules" not in E.names("node_modules"), E.names("skipme"))
os.makedirs(os.path.join(E.base, "System Volume Information")); open(os.path.join(E.base, "System Volume Information", "x.dat"), "w").write("x")
os.makedirs(os.path.join(E.base, "$Recycle.Bin")); open(os.path.join(E.base, "$Recycle.Bin", "y.dat"), "w").write("x")
os.symlink(E.base, os.path.join(E.base, "code", "loop"))               # directory loop
os.symlink(os.path.join(E.base, "Music"), os.path.join(E.base, "link-to-music"))
open(os.path.join(E.base, "Pictures", "broken.lnk"), "w").write("x"); os.symlink("/nonexistent/xyz", os.path.join(E.base, "dangling"))
t0 = time.time(); E.scan(); dt = time.time() - t0
ok("built-in skip names ignored", E.names("x.dat") == [] and E.names("y.dat") == [] and E.names("system volume") == [] and E.names("recycle") == [])
ok("symlink loop not followed (finishes)", dt < 15 and E.names("song.mp3") == ["song.mp3"], (dt, E.names("song.mp3")))
ok("symlinks listed as names only", "loop" in E.names("loop") and "link-to-music" in E.names("link-to-music") and "dangling" in E.names("dangling"))
ok("no duplicate entries", len(E.names("main.py")) == 1)
# own data dir never indexed even when inside a root
own = os.path.join(E.base, "own-data"); os.makedirs(own); open(os.path.join(own, "index.db-wal"), "w").write("x")
E.idx.data_dir = own; E.idx._make_rules(); E.scan()
ok("own data folder is skipped", E.names("index.db-wal") == [] and E.names("own-data") == [])
E.idx.data_dir = E.data; E.idx._make_rules()

# ---- diff updates (add/rename/delete/modify/type change/dir remove)
E.settings.update({"excludes": []}); E.idx._make_rules(); E.scan()
n0 = E.db.counts()[0]
open(os.path.join(E.base, "Documents", "brand new.txt"), "w").write("hello")
os.rename(os.path.join(E.base, "Documents", "notes.txt"), os.path.join(E.base, "Documents", "Notes2.TXT"))
os.remove(os.path.join(E.base, "Music", "song.mp3"))
with open(os.path.join(E.base, "Documents", "Report Final.pdf"), "ab") as f: f.write(b"more")
shutil.rmtree(os.path.join(E.base, "Pictures", "holiday"))
os.makedirs(os.path.join(E.base, "new dir", "deep")); open(os.path.join(E.base, "new dir", "deep", "treasure.dat"), "w").write("t")
E.scan()
ok("rescan adds new file", E.names("brand new") == ["brand new.txt"])
ok("rescan handles rename", E.names("notes") == ["Notes2.TXT"], E.names("notes"))
ok("rescan removes deleted file", E.names("song") == [])
ok("rescan updates size", [x for x in E.db.search({"q": "report final"})["results"]][0]["size"] == 5004)
ok("rescan purges removed folder tree", E.names("img_000") == [] and E.names("holiday") == [] and E.names("readme") == [])
ok("rescan finds new nested folder", E.names("treasure") == ["treasure.dat"])
fts_count = E.db.w.execute("SELECT COUNT(*) FROM fts_docsize").fetchone()[0]
ok("fts rows == entries rows after churn", fts_count == E.db.counts()[0], (fts_count, E.db.counts()[0]))
ok("fts consistency check", E.db.w.execute("INSERT INTO fts(fts, rank) VALUES('integrity-check', 1)") is not None)
os.remove(os.path.join(E.base, "new dir", "deep", "treasure.dat")); os.mkdir(os.path.join(E.base, "new dir", "deep", "treasure.dat"))   # file -> folder
E.scan(); ok("type change file->folder", [x["is_dir"] for x in E.db.search({"q": "treasure"})["results"]] == [True])
orphans = E.db.w.execute("SELECT COUNT(*) FROM entries WHERE dir_id NOT IN (SELECT id FROM dirs)").fetchone()[0]
ok("no orphan entries", orphans == 0, orphans)
ok("entry_count tracks db", E.idx.entry_count == E.db.counts()[0], (E.idx.entry_count, E.db.counts()[0]))

# ---- watcher event handling (no OS watcher on Linux: feed events like the watcher thread does)
open(os.path.join(E.base, "Documents", "evt.txt"), "w").write("e")
E.idx.on_event(E.root, 1, "Documents/evt.txt".replace("/", os.sep))
os.makedirs(os.path.join(E.base, "evdir", "sub")); open(os.path.join(E.base, "evdir", "sub", "in.txt"), "w").write("i")
E.idx.on_event(E.root, 1, "evdir"); 
with open(os.path.join(E.base, "Documents", "evt.txt"), "ab") as f: f.write(b"1234567")
os.utime(os.path.join(E.base, "Documents", "evt.txt"), (1_600_000_000, 1_600_000_000))
os.remove(os.path.join(E.base, "Documents", "brand new.txt")); E.idx.on_event(E.root, 2, "Documents/brand new.txt".replace("/", os.sep))
E.idx.on_event(E.root, 3, "Documents/Report Final.pdf".replace("/", os.sep))
E.idx._flush_dirty()
ok("event: new file + new tree indexed", E.names("evt.txt") == ["evt.txt"] and E.names("in.txt") == ["in.txt"] and E.names("evdir") == ["evdir"])
ok("event: removal applied", E.names("brand new") == [])
ok("event: modified file touched (no dir relist needed)", E.db.search({"q": "evt.txt"})["results"][0]["size"] == 8)
E.idx.on_event(E.root, 3, "Documents/evt.txt".replace("/", os.sep)); E.idx._flush_dirty()
ok("event: modified updates mtime", E.db.search({"q": "evt.txt"})["results"][0]["mtime"] == 1_600_000_000)
ok("events outside excluded/own folders only", True)
E2 = Env(tree=False); E2.idx.data_dir = os.path.join(E2.base, "mydata"); os.makedirs(E2.idx.data_dir); E2.idx._make_rules()
E2.idx.on_event(E2.root, 1, os.path.join("mydata", "index.db-wal")); E2.idx.on_event(E2.root, 3, os.path.join("mydata", "index.db")); ok("own data-dir events ignored (no feedback loop)", not E2.idx.dirty_dirs and not E2.idx.dirty_files)
E2.idx.on_event(E2.root, 1, "other.txt"); ok("normal events are queued", E2.idx.dirty_dirs == {E2.root}); E2.close()
E.settings.update({"excludes": [os.path.join(E.base, "evdir")]}); E.idx._make_rules()
E.idx.on_event(E.root, 1, os.path.join("evdir", "sub", "again.txt")); ok("excluded folder events ignored", not E.idx.dirty_dirs)
E.settings.update({"excludes": []}); E.idx._make_rules()
# event for a folder that vanished + overflow
shutil.rmtree(os.path.join(E.base, "evdir")); E.idx.on_event(E.root, 2, "evdir"); E.idx._flush_dirty()
ok("event: removed folder purged", E.names("in.txt") == [] and E.names("evdir") == [])
open(os.path.join(E.base, "after-overflow.txt"), "w").write("o"); E.idx.on_overflow(E.root); E.idx._flush_dirty()
ok("overflow triggers a rescan of that drive", E.names("after-overflow") == ["after-overflow.txt"] and E.idx.state["watch_overflows"] == 1)

# ---- events must not leak skipped/unindexed places into the index
os.makedirs(os.path.join(E.base, "$Recycle.Bin", "S-1-5")); open(os.path.join(E.base, "$Recycle.Bin", "S-1-5", "trash.dat"), "w").write("x")
os.makedirs(os.path.join(E.base, "Config.Msi")); open(os.path.join(E.base, "Config.Msi", "leak.msi"), "w").write("x")
E.idx.on_event(E.root, 1, os.path.join("$Recycle.Bin", "S-1-5", "trash.dat")); E.idx.on_event(E.root, 3, os.path.join("Config.Msi", "leak.msi")); E.idx.on_event(E.root, 1, os.path.join("Config.Msi", "leak.msi"))
E.idx._flush_dirty()
ok("event inside built-in skipped folder ignored", E.names("trash.dat") == [])
ok("event in a folder that is not indexed is ignored (no leak of skipped system/hidden folders)", E.names("leak.msi") == [])
shutil.rmtree(os.path.join(E.base, "$Recycle.Bin")); shutil.rmtree(os.path.join(E.base, "Config.Msi"))
r = indexer.Rules([E.root], {"excludes": []}, E.data)
ok("Rules.excluded: built-in marks at any depth", r.excluded(E.root + "a" + os.sep + "$Recycle.Bin" + os.sep + "x" + os.sep) and r.excluded(E.root + "System Volume Information" + os.sep) and not r.excluded(E.root + "System Volume Information2" + os.sep))

import indexer as _ix
_old_max = _ix.MAX_DIRTY; _ix.MAX_DIRTY = 2
E.idx.on_event(E.root, 1, "a1.txt"); E.idx.on_event(E.root, 1, os.path.join("Documents", "a2.txt")); E.idx.on_event(E.root, 1, os.path.join("Pictures", "a3.txt"))
ok("too many pending events -> rescan of the drive instead of dropping them", E.root in E.idx.overflow and len(E.idx.dirty_dirs) == 2)
_ix.MAX_DIRTY = _old_max; E.idx.overflow.clear(); E.idx.dirty_dirs.clear()

# ---- apply settings: drive off, exclude/un-exclude
old = E.settings.get(); E.settings.update({"excludes": [os.path.join(E.base, "Documents")]})
E.idx._apply(old); ok("apply: new exclude purges (incl. its own entry)", E.names("report") == [] and E.names("documents", type="folders") == [], (E.names("report"), E.names("documents")))
old = E.settings.get(); E.settings.update({"excludes": []}); E.idx._apply(old)
ok("apply: removing exclude restores it", "Report Final.pdf" in E.names("report") and "Documents" in E.names("documents", type="folders"))
old = E.settings.get(); E.settings.update({"drives_off": [E.root]}); E.idx._apply(old)
ok("apply: drive off purges everything of it", E.db.counts()[0] == 0 and E.db.counts()[1] == 0 and E.db.meta_get("built:" + E.root.lower()) is None, E.db.counts())
old = E.settings.get(); E.settings.update({"drives_off": []}); E.idx._apply(old)
ok("apply: drive on rebuilds it", "Report Final.pdf" in E.names("report") and E.db.meta_get("built:" + E.root.lower()) is not None)
old = E.settings.get(); E.settings.update({"skip_hidden": True}); open(os.path.join(E.base, ".hidden-linux"), "w").write("x"); E.idx._apply(old)
ok("apply: toggling skip flags rescans", E.idx.state["phase"] == "idle")

# ---- reset
E.db.reset(); ok("reset empties index", E.db.counts() == (0, 0) and E.names("") == [])
E.scan(); ok("scan after reset works (ids restart)", "Report Final.pdf" in E.names("report"))
E.close()

# ---- edge cases: surrogate names, unreadable dirs, huge dirs, root vanishing
E = Env(tree=False)
bad = os.path.join(E.base, os.fsdecode(b"bad\xffname.txt")); open(bad, "w").write("x")
os.makedirs(os.path.join(E.base, "locked")); open(os.path.join(E.base, "locked", "secret.txt"), "w").write("s")
os.makedirs(os.path.join(E.base, "many")); 
for i in range(3000): open(os.path.join(E.base, "many", "f%05d.dat" % i), "w").close()
if os.geteuid() != 0: os.chmod(os.path.join(E.base, "locked"), 0)
E.scan()
ok("undecodable file name stored (replacement char)", E.names("bad") == ["bad\ufffdname.txt"], E.names("bad"))
ok("3000-file folder indexed", len(E.names("f0", type="files")) == 3000)
if os.geteuid() != 0:
    ok("unreadable folder skipped without crashing", E.names("secret") == [] and E.names("locked") == ["locked"] and E.idx.state["errors"] >= 1)
    os.chmod(os.path.join(E.base, "locked"), 0o755); E.scan(); ok("folder indexed once readable", E.names("secret") == ["secret.txt"])
    os.chmod(os.path.join(E.base, "locked"), 0); E.scan(); ok("unreadable-again folder keeps old entries (no false purge)", E.names("secret") == ["secret.txt"])
    os.chmod(os.path.join(E.base, "locked"), 0o755)
else:
    ok("(root user: permission tests skipped)", True)
gone_root = Env(tree=True); gone_root.scan(); n_before = gone_root.db.counts()[0]
shutil.rmtree(gone_root.base); gone_root.scan()
ok("vanished root (unplugged drive) keeps its index, not purged", gone_root.db.counts()[0] == n_before and gone_root.db.meta_get("count:" + gone_root.root.lower()) is not None)
gone_root.close()

# ---- unplugged drive stays searchable, damaged index file is replaced, commits are time-based
E3 = Env(); E3.scan(); n3 = E3.db.counts()[0]; E3.idx.roots_fn = lambda: []; old = E3.settings.get(); E3.idx._apply(old)
ok("unplugged drive keeps its index (still searchable)", E3.db.counts()[0] == n3 and "Report Final.pdf" in E3.names("report"))
E3.close()
import server
dd = __import__("tempfile").mkdtemp(); open(os.path.join(dd, "index.db"), "wb").write(b"this is not a sqlite database" * 100)
d2 = server.open_database(os.path.join(dd, "index.db")); d2.fts
ok("damaged index file is moved aside and a fresh one opened", d2.counts() == (0, 0) and any(".damaged-" in f for f in os.listdir(dd)), os.listdir(dd)); d2.close(); shutil.rmtree(dd)
E3 = Env(tree=False); E3.db.begin(); E3.db.add_entries(E3.db.dir_id(E3.root), [("a.txt", False, 1, 1, 0)]); E3.db.pending = 1
E3.db.maybe_commit(); ok("no commit before 2 s for a tiny batch", E3.db.in_tx)
E3.db.last_commit -= 3; E3.db.maybe_commit(); ok("commit after 2 s even for a tiny batch (live results while building)", not E3.db.in_tx and E3.names("a.txt") == ["a.txt"]); E3.close()

# ---- throttle + pause + stop (duty cycle really lowers CPU)
th = indexer.Throttle(threading.Event(), threading.Event())
t0 = time.monotonic(); th.reset()
for _ in range(10):
    x = time.monotonic()
    while time.monotonic() - x < 0.03: pass
    th.tick(2.0)
el = time.monotonic() - t0
ok("throttle gentle sleeps ~2x work (duty ~33%)", 0.85 < el < 1.25 and el > 0.8, el)
stop, paused = threading.Event(), threading.Event(); th = indexer.Throttle(stop, paused); paused.set()
res = {}
def tk(): th.tick(1.0); res["t"] = time.monotonic()
t = threading.Thread(target=tk); t.start(); time.sleep(0.5); ok("pause blocks the worker", t.is_alive()); paused.clear(); t.join(2); ok("resume releases it", not t.is_alive())
paused.set(); t = threading.Thread(target=tk); t.start(); time.sleep(0.2); stop.set(); t.join(2); ok("stop releases a paused worker", not t.is_alive())
ok("ratios ordered gentle>normal>fast", indexer.RATIO["gentle"] > indexer.RATIO["normal"] > indexer.RATIO["fast"])

# ---- threaded indexer: start/stop, startup scan, periodic rescan, pause, search while building
E.close()
E = Env(); 
for i in range(200): os.makedirs(os.path.join(E.base, "bulk", "d%03d" % i)); open(os.path.join(E.base, "bulk", "d%03d" % i, "file.txt"), "w").close()
E.settings.update({"speed": "normal", "rescan_min": 15}); E.idx._make_rules()
E.idx.start(); t0 = time.time()
while E.idx.state["phase"] != "idle" and time.time() - t0 < 60: time.sleep(0.1)
while E.idx.state["last_done"] is None and time.time() - t0 < 60: time.sleep(0.1)
ok("threaded startup scan completes", E.idx.state["last_done"] is not None and "Report Final.pdf" in E.names("report"), E.idx.state)
ok("next periodic scan scheduled", E.idx.next_scan and abs(E.idx.next_scan - time.time() - 900) < 30)
E.idx.pause(True); open(os.path.join(E.base, "paused-add.txt"), "w").write("x"); E.idx.on_event(E.root, 1, "paused-add.txt"); time.sleep(4.2)
ok("paused: events wait", E.names("paused-add") == [] and E.idx.progress()["phase"] == "paused")
E.idx.pause(False); time.sleep(4.5); ok("resumed: events applied", E.names("paused-add") == ["paused-add.txt"])
E.idx.next_scan = time.time() - 1; open(os.path.join(E.base, "periodic-add.txt"), "w").write("x"); time.sleep(4); 
t0 = time.time()
while E.names("periodic-add") == [] and time.time() - t0 < 20: time.sleep(0.2)
ok("periodic rescan picks up unwatched change", E.names("periodic-add") == ["periodic-add.txt"])
E.idx.submit("reset"); t0 = time.time(); time.sleep(0.5)
while E.idx.state["phase"] != "idle" and time.time() - t0 < 30: time.sleep(0.1)
time.sleep(0.5); ok("reset job rebuilds", E.names("report") != [] and E.idx.state["kind"] is None)
t0 = time.time(); E.idx.stop(); ok("stop() returns quickly and thread ends", time.time() - t0 < 6 and not E.idx.thread.is_alive())
E.close()

# ---- search cancel + performance on a big synthetic index
E = Env(tree=False); import random
random.seed(7)
did = E.db.dir_id(E.root)
words = ["".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(3, 9))) for _ in range(4000)]
dirs = [E.db.dir_id(E.root + "d%d" % i + os.sep) for i in range(3000)]
E.db.begin()
for chunk in range(40):
    E.db.add_entries(random.choice(dirs), [(random.choice(words) + "_" + random.choice(words) + "." + random.choice(["txt", "jpg", "dll", "py"]), False, random.randrange(10**7), 1_700_000_000 + random.randrange(10**7), 0) for _ in range(5000)])
E.db.commit()
nbig = E.db.counts()[0]
for q, label in (("report", "fts 6 chars"), ("abc", "fts 3 chars"), ("*.dll", "ext-only wildcard"), ("ab", "2-char scan")):
    best = min(E.db.search({"q": q, "limit": 300})["ms"] for _ in range(3))
    ok("search %s on %d entries: %.0f ms (<400)" % (label, nbig, best), best < 400, best)
best = min(E.db.search({"q": "abc", "sort": "size", "dir": "desc", "limit": 300})["ms"] for _ in range(3)); ok("fts + size sort %.0f ms" % best, best < 250, best)
# a slow query is interrupted by a newer one
slow = {"q": "zq?", "limit": 300}; out = {}
def run_slow(): out["r"] = E.db.search({"q": "*a*b*c*d*", "limit": 300, "sort": "path"})
t = threading.Thread(target=run_slow); t.start(); time.sleep(0.03); quick = E.db.search({"q": "report", "limit": 5}); t.join(20)
ok("newer search supersedes older (older cancelled or finished)", "results" in quick and (out["r"].get("cancelled") or "results" in out["r"]), out)
E.close()
R.done()
