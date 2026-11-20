"""Server tests (box): endpoints, security rules, open/reveal gating, settings API, launcher smoke test."""
import json, os, re, subprocess, sys, tempfile, threading, time, urllib.request, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import *
R = Results(); ok = R.ok
PORT = 18771; os.environ["QUICKFIND_PORT"] = str(PORT)
E = Env(); os.environ["QUICKFIND_DATA"] = E.data
import server, winshell, db as dbmod
calls = []
winshell.open_path = lambda p: (calls.append(("open", p)) or True)
winshell.reveal = lambda p: (calls.append(("reveal", p)) or True)
winshell.IS_WIN = False
server.init(database=E.db, settings=E.settings, idx=E.idx)
E.idx.watcher_cls = None
server.on_start()
httpd = server.build_server(PORT); threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % PORT
def req(path, method="GET", body=None, headers=None):
    r = urllib.request.Request(URL + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp: return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e: return e.code, e.read(), dict(e.headers)
def jget(path):
    s, b, h = req(path); return s, json.loads(b)
t0 = time.time()
while E.idx.state["last_done"] is None and time.time() - t0 < 30: time.sleep(0.1)
s, page, hdr = req("/"); token = re.search(rb'name="app-token" content="([^"]+)"', page).group(1).decode()
J = {"Content-Type": "application/json", "X-App-Token": token}
def post(path, obj, headers=None): s, b, h = req(path, "POST", json.dumps(obj).encode(), headers or J); return s, json.loads(b)

ok("index page served with token injected", s == 200 and len(token) > 20 and b"__APP_TOKEN__" not in page)
ok("strict CSP", "default-src 'self'" in hdr["Content-Security-Policy"] and "script-src 'self'" in hdr["Content-Security-Policy"] and "unsafe-inline" not in hdr["Content-Security-Policy"] and "frame-ancestors 'none'" in hdr["Content-Security-Policy"])
ok("no-store + nosniff", hdr["Cache-Control"] == "no-store" and hdr["X-Content-Type-Options"] == "nosniff")
ok("no inline script/style/handlers in html", not re.search(rb"<script(?![^>]*src=)[^>]*>|style=|onclick=|onerror=|javascript:", page))
for f in ("app.js", "core.js", "styles.css", "app.css", "favicon.svg"):
    ok("static " + f, req("/" + f)[0] == 200)
_js = re.sub(r"/\*.*?\*/", "", open(os.path.join(ROOT, "web", "app.js")).read() + open(os.path.join(ROOT, "web", "core.js")).read(), flags=re.S)
ok("no innerHTML/eval/document.write in JS", not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", _js))
ok("path traversal blocked", req("/..%2f..%2fserver.py")[0] == 404 and req("/../server.py")[0] == 404 and req("/%2e%2e/server.py")[0] == 404)
ok("bad Host header rejected", req("/api/status", headers={"Host": "evil.example"})[0] == 403 and req("/", headers={"Host": "127.0.0.1:1"})[0] == 403)
ok("localhost Host accepted", req("/api/status", headers={"Host": "localhost:%d" % PORT})[0] == 200)
s, st = jget("/api/status")
ok("status fields", s == 200 and st["version"] == "1.0.0" and st["index"]["entries"] >= 20 and st["index"]["fts"] is True and st["settings"]["speed"] == "gentle" and st["drives"][0]["root"] == E.root and st["drives"][0]["built"] is True, st)
s, r = jget("/api/search?q=report&limit=10"); ok("search endpoint", s == 200 and {x["name"] for x in r["results"]} == {"Report Final.pdf", "report_draft.docx"} and "ms" in r)
ok("search: type/ext/size/date/sort params", jget("/api/search?q=&type=images&sort=size&dir=desc")[1]["results"][0]["name"] == "IMG_0001.JPG" and jget("/api/search?ext=pdf&smin=1000")[1]["results"][0]["name"] == "Report Final.pdf")
ok("search: bad params -> 400", jget("/api/search?type=zzz")[0] == 400 and jget("/api/search?sort=zzz")[0] == 400 and jget("/api/search?smin=abc")[0] == 400)
ok("search: limit clamped", len(jget("/api/search?q=*&limit=1")[1]["results"]) == 1 and jget("/api/search?q=*&limit=99999999")[1]["limit"] == dbmod.MAX_LIMIT)
ok("search: unicode query via url", jget("/api/search?q=" + urllib.request.quote("ファイル"))[1]["results"][0]["name"].endswith(".txt"))
ok("search: very long query ok", jget("/api/search?q=" + "a" * 5000)[0] == 200)
ok("search: weird glob chars ok", all(jget("/api/search?q=" + urllib.request.quote(q))[0] == 200 for q in ["[", "[]", "a[b", "***", "?*?", '"', '""', '!', '!"', "\\", "/", "*/*", "%", "_", "'"]))
ok("GET unknown api 404", jget("/api/nope")[0] == 404)
item = jget("/api/search?q=notes.txt")[1]["results"][0]
ok("POST without token rejected", req("/api/open", "POST", json.dumps({"id": item["id"]}).encode(), {"Content-Type": "application/json"})[0] == 403 and not calls)
ok("POST wrong token rejected", req("/api/open", "POST", b"{}", dict(J, **{"X-App-Token": "x" * 32}))[0] == 403)
ok("POST wrong content-type rejected", req("/api/open", "POST", b"{}", {"X-App-Token": token, "Content-Type": "text/plain"})[0] == 415)
ok("POST bad json / non-object", req("/api/open", "POST", b"{nope", J)[0] == 400 and req("/api/open", "POST", b"[1]", J)[0] == 400)
ok("POST huge body rejected", req("/api/open", "POST", b"{" + b" " * 300000 + b"}", J)[0] == 413)
ok("POST bad host rejected", req("/api/open", "POST", b"{}", dict(J, Host="evil.example"))[0] == 403)
ok("open: only by id (path in body is ignored/invalid)", post("/api/open", {"path": "/etc/passwd"})[0] == 400 and post("/api/open", {"id": "../../etc"})[0] == 400 and not calls)
ok("open: unknown id 404", post("/api/open", {"id": 99999999})[0] == 404)
s, r = post("/api/open", {"id": item["id"]}); ok("open file by id", s == 200 and r["ok"] and calls[-1] == ("open", os.path.join(E.base, "Documents", "notes.txt")), (r, calls))
s, r = post("/api/reveal", {"id": item["id"]}); ok("reveal by id", r["ok"] and calls[-1][0] == "reveal")
# exec + cloud gating
open(os.path.join(E.base, "setup.exe"), "w").write("x"); open(os.path.join(E.base, "run.PS1"), "w").write("x")
E.idx.sync_dir(E.root, False); E.db.commit()
ex = jget("/api/search?q=setup.exe")[1]["results"][0]; n_calls = len(calls)
s, r = post("/api/open", {"id": ex["id"]}); ok("open exe needs confirmation", r.get("confirm") == "exec" and len(calls) == n_calls, r)
ok("confirm flag must be literally true", post("/api/open", {"id": ex["id"], "confirm_exec": "yes"})[1].get("confirm") == "exec" and len(calls) == n_calls)
s, r = post("/api/open", {"id": ex["id"], "confirm_exec": True}); ok("open exe with confirmation", r["ok"] and len(calls) == n_calls + 1)
ps = jget("/api/search?q=run.ps1")[1]["results"][0]; ok("ps1 also gated", post("/api/open", {"id": ps["id"]})[1].get("confirm") == "exec")
did = E.db.dir_id(E.root); E.db.add_entries(did, [("cloudy.docx", False, 100, 1700000000, dbmod.FLAG_CLOUD)]); E.db.commit()
open(os.path.join(E.base, "cloudy.docx"), "w").write("x")
cl = jget("/api/search?q=cloudy")[1]["results"][0]; n_calls = len(calls)
ok("online-only flag exposed", cl["flags"] & 4)
s, r = post("/api/open", {"id": cl["id"]}); ok("online-only open needs download confirmation", r.get("confirm") == "download" and len(calls) == n_calls)
s, r = post("/api/open", {"id": cl["id"], "allow_download": True}); ok("online-only open after consent", r["ok"])
os.remove(os.path.join(E.base, "Documents", "notes.txt")); n_calls = len(calls)
s, r = post("/api/open", {"id": item["id"]}); ok("stale entry: clear error, nothing launched, folder queued", not r["ok"] and "no longer exists" in r["error"] and len(calls) == n_calls and E.idx.dirty_dirs, r)
t0 = time.time()
while jget("/api/search?q=notes.txt")[1]["results"] and time.time() - t0 < 15: time.sleep(0.3)
ok("stale entry disappears after the re-read", jget("/api/search?q=notes.txt")[1]["results"] == [])
# settings API
s, r = post("/api/settings", {"speed": "turbo"}); ok("settings: invalid -> 400", s == 400)
s, r = post("/api/settings", {"excludes": ["relative"]}); ok("settings: relative path -> 400", s == 400)
s, r = post("/api/settings", {"excludes": [os.path.join(E.base, "Music")], "speed": "fast", "rescan_min": 15, "evil": 1})
ok("settings saved, unknown keys ignored", s == 200 and r["settings"]["speed"] == "fast" and "evil" not in r["settings"], r)
t0 = time.time()
while jget("/api/search?q=song.mp3")[1]["results"] and time.time() - t0 < 15: time.sleep(0.3)
ok("excluded folder disappears from search", jget("/api/search?q=song.mp3")[1]["results"] == [] and jget("/api/search?q=music&type=folders")[1]["results"] == [])
ok("settings file written", json.load(open(os.path.join(E.data, "settings.json")))["speed"] == "fast")
post("/api/settings", {"excludes": []}); 
t0 = time.time()
while not jget("/api/search?q=song.mp3")[1]["results"] and time.time() - t0 < 15: time.sleep(0.3)
ok("un-excluding brings it back", jget("/api/search?q=song.mp3")[1]["results"] != [])
ok("rescan + pause endpoints", post("/api/rescan", {})[1]["ok"] and post("/api/pause", {"paused": True})[1]["paused"] is True and jget("/api/status")[1]["index"]["phase"] == "paused" and post("/api/pause", {"paused": False})[1]["paused"] is False)
ok("rebuild needs confirm:true", post("/api/rebuild", {})[0] == 400 and post("/api/rebuild", {"confirm": "yes"})[0] == 400)
s, r = post("/api/pick-folder", {}); ok("pick-folder graceful off Windows", s == 200 and r["ok"] is False)
time.sleep(1.5)
ok("rebuild endpoint works", post("/api/rebuild", {"confirm": True})[1]["ok"])
t0 = time.time(); time.sleep(0.5)
while (jget("/api/status")[1]["index"]["phase"] != "idle" or not jget("/api/search?q=report")[1]["results"]) and time.time() - t0 < 30: time.sleep(0.3)
ok("rebuilt index searchable again", len(jget("/api/search?q=report")[1]["results"]) == 2)
httpd.shutdown(); server.on_stop()
ok("on_stop returns, thread ended", not E.idx.thread.is_alive())
E.close()

# ---- launcher smoke test (real entry point, own data dir + tiny tree, never touches the real index)
tmp = tempfile.mkdtemp(prefix="qf-l-"); tree = make_tree(os.path.join(tmp, "t")); data = os.path.join(tmp, "data")
env = dict(os.environ, QUICKFIND_DATA=data, QUICKFIND_ROOTS=tree, QUICKFIND_PORT="")
env.pop("QUICKFIND_PORT")
p = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py"), "--no-window", "--port", "18772"], env=env, cwd=ROOT)
try:
    base = "http://127.0.0.1:18772"
    for _ in range(100):
        try: urllib.request.urlopen(base + "/api/status", timeout=2); break
        except Exception: time.sleep(0.2)
    for _ in range(100):
        r = json.loads(urllib.request.urlopen(base + "/api/search?q=report", timeout=5).read())
        if len(r["results"]) == 2: break
        time.sleep(0.2)
    ok("launcher: serves and indexes the roots", len(r["results"]) == 2, r)
    ok("launcher: data in QUICKFIND_DATA", os.path.isfile(os.path.join(data, "index.db")) and os.path.isfile(os.path.join(data, "quickfind.log")))
    p2 = subprocess.run([sys.executable, os.path.join(ROOT, "launcher.py"), "--no-window", "--port", "18772"], env=env, cwd=ROOT, timeout=20)
    ok("launcher: second instance on busy port exits 0 (already running)", p2.returncode == 0)
finally:
    p.terminate()
    try: p.wait(10)
    except Exception: p.kill()
ok("launcher: terminates cleanly", p.returncode is not None)
shutil.rmtree(tmp, ignore_errors=True)
R.done()
