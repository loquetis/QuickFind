"""Windows-only checks (run on CUBE-ALIEN with the build python, against temp folders only):
file attributes, junction loop, long paths, unicode, the real ReadDirectoryChangesW watcher, background thread mode.
Run: python tests\\test_windows.py"""
import os, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import *
R = Results(); ok = R.ok
if os.name != "nt":
    print("skipped: Windows only"); raise SystemExit(0)
import storage, db as dbmod, indexer, watcher, winshell
def sh(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True)
tmp = tempfile.mkdtemp(prefix="qf-win-"); base = os.path.join(tmp, "tree"); data = os.path.join(tmp, "data"); os.makedirs(base); os.makedirs(data)
def w(rel, data=b"x"):
    p = os.path.join(base, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(data)
    return p
try:
    # path helpers
    ok("norm_folder drive path", storage.norm_folder("c:\\Users\\demo\\") == "c:\\Users\\demo" and storage.norm_folder("C:\\") == "C:\\" and storage.norm_folder('"D:\\a\\..\\b"') == "D:\\b", storage.norm_folder("c:\\Users\\demo\\"))
    for bad in ("relative\\x", "\\rooted", "C:rel", ""):
        try: storage.norm_folder(bad); ok("norm_folder rejects %r" % bad, False)
        except ValueError: ok("norm_folder rejects %r" % bad, True)
    ok("norm_root adds separator", storage.norm_root("D:\\x") == "D:\\x\\" and storage.norm_root("C:\\") == "C:\\")
    drives = winshell.fixed_drives(); ok("fixed drives found", len(drives) >= 1 and all(len(d) == 3 for d in drives), drives)
    ok("volume label call works", isinstance(winshell.volume_label(drives[0]), str))
    res = {}
    t = __import__("threading").Thread(target=lambda: res.update(bg=winshell.set_background_mode())); t.start(); t.join()
    ok("background thread mode (low CPU+disk priority) accepted", res.get("bg") is True)
    # tree with special cases
    w("Docs/Report.TXT"); w("Docs/ünï çödé ファイル.txt"); w("hidden/h.txt"); w("sys/s.txt"); w("Docs/visible.txt")
    long_dir = base
    for i in range(7): long_dir = os.path.join(long_dir, "a_rather_long_folder_name_%d_xxxxxxxxxxxx" % i)
    lp = winshell.long_path(os.path.join(long_dir, "deep-file-in-long-path.txt")); os.makedirs(winshell.long_path(long_dir)); open(lp, "w").write("x")
    ok("long path fixture is > 260 chars", len(long_dir) > 260, len(long_dir))
    sh('attrib +h "%s"' % os.path.join(base, "hidden", "h.txt")); sh('attrib +s +h "%s"' % os.path.join(base, "sys", "s.txt"))
    os.makedirs(os.path.join(base, "junction-target")); w("junction-target/inside-target.txt")
    r = sh('mklink /J "%s" "%s"' % (os.path.join(base, "junction-target", "loop-back"), base)); ok("junction created (no admin needed)", r.returncode == 0, r.stdout + r.stderr)
    r = sh('mklink /J "%s" "%s"' % (os.path.join(base, "junction-to-target"), os.path.join(base, "junction-target"))); ok("second junction created", r.returncode == 0)
    root = storage.norm_root(base)
    st = storage.Settings(os.path.join(data, "settings.json")); st.update({"speed": "fast", "rescan_min": 0})
    d = dbmod.Database(os.path.join(data, "index.db"))
    idx = indexer.Indexer(d, st, roots_fn=lambda: [root], watcher_cls=watcher.Watcher, data_dir=data); idx._make_rules()
    def names(q, **kw): return [x["name"] for x in d.search(dict(q=q, limit=500, **kw))["results"]]
    t0 = time.time(); idx._scan_all("manual"); dt = time.time() - t0
    ok("scan finishes (junction loops not followed)", dt < 30, dt)
    ok("basic search + case-insensitive", names("report") == ["Report.TXT"], names("report"))
    ok("unicode names", names("ファイル") == ["ünï çödé ファイル.txt"] and names("ÜNÏ") == ["ünï çödé ファイル.txt"], names("ファイル"))
    ok("long path file indexed (>260 chars)", names("deep-file") == ["deep-file-in-long-path.txt"], names("deep-file"))
    res = d.search({"q": "deep-file"})["results"]; ok("long path stored without \\\\?\\ prefix", res and not res[0]["folder"].startswith("\\\\?\\") and len(res[0]["folder"]) > 260)
    ok("system file skipped by default", names("s.txt") == [], names("s.txt"))
    ok("hidden (non-system) file indexed when skip_hidden is off", "h.txt" in names("h.txt"), names("h.txt"))
    ok("junctions listed as names only, not followed", names("junction-to-target") == ["junction-to-target"] and names("inside-target") == ["inside-target.txt"] and names("loop-back") == ["loop-back"], (names("inside-target"), names("loop-back")))
    ok("junction flagged as link", [x["flags"] & 8 for x in d.search({"q": "junction-to-target"})["results"]] == [8])
    # skip_hidden
    old = st.get(); st.update({"skip_hidden": True}); idx._apply(old)
    ok("skip_hidden removes hidden files after apply", "h.txt" not in names("h.txt"), names("h.txt"))
    old = st.get(); st.update({"skip_hidden": False}); idx._apply(old)
    # live watcher
    idx.start()
    t0 = time.time()
    while idx.state["last_done"] is None and time.time() - t0 < 30: time.sleep(0.2)
    time.sleep(1.5)
    ok("watcher thread running", [x["state"] for x in idx.watcher_states()] == ["watching"], idx.watcher_states())
    def waitfor(cond, sec=15):
        t0 = time.time()
        while time.time() - t0 < sec:
            if cond(): return time.time() - t0
            time.sleep(0.3)
        return None
    open(os.path.join(base, "Docs", "live-new-file.txt"), "w").write("hello")
    dt = waitfor(lambda: names("live-new-file") == ["live-new-file.txt"]); ok("watcher: new file appears (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    os.rename(os.path.join(base, "Docs", "live-new-file.txt"), os.path.join(base, "Docs", "live-renamed.txt"))
    dt = waitfor(lambda: names("live-renamed") == ["live-renamed.txt"] and names("live-new-file") == []); ok("watcher: rename applied (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    with open(os.path.join(base, "Docs", "live-renamed.txt"), "ab") as f: f.write(b"x" * 5000)
    dt = waitfor(lambda: [x["size"] for x in d.search({"q": "live-renamed"})["results"]] == [5005]); ok("watcher: size change applied (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    os.remove(os.path.join(base, "Docs", "live-renamed.txt"))
    dt = waitfor(lambda: names("live-renamed") == []); ok("watcher: delete applied (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    os.makedirs(os.path.join(base, "newtree", "deeper")); open(os.path.join(base, "newtree", "deeper", "tree-file.txt"), "w").write("x")
    dt = waitfor(lambda: names("tree-file") == ["tree-file.txt"]); ok("watcher: new folder tree indexed (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    shutil.rmtree(os.path.join(base, "newtree"))
    dt = waitfor(lambda: names("tree-file") == [] and names("newtree") == [] and names("deeper") == []); ok("watcher: folder tree removal applied (%s s)" % (round(dt, 1) if dt else "never"), dt is not None)
    open(os.path.join(base, "Docs", "live-sys.txt"), "w").write("x"); sh('attrib +s +h "%s"' % os.path.join(base, "Docs", "live-sys.txt"))
    time.sleep(6); ok("watcher: new system-attribute file is not indexed", names("live-sys") == [])
    ev = idx.state["events"]; open(os.path.join(data, "index.db-wal-test"), "w").write("x") ; time.sleep(1)
    ok("events counted", ev > 0, ev)
    t0 = time.time(); idx.stop(); ok("stop() joins watcher + indexer quickly", time.time() - t0 < 8 and not idx.thread.is_alive(), time.time() - t0)
    d.close()
finally:
    shutil.rmtree(winshell.long_path(tmp), ignore_errors=True)
R.done()
