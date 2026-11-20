"""UI test on the box: headless Chrome against the real server + a temp tree (in-process, open/reveal patched).
Run: /workspace/_testenv/bin/python tests/uitest.py"""
import json, os, re, sys, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import *
from playwright.sync_api import sync_playwright
R = Results(); ok = R.ok
PORT = 18773; os.environ["QUICKFIND_PORT"] = str(PORT)
E = Env(); os.environ["QUICKFIND_DATA"] = E.data
HOSTILE = '<img src=x onerror=window.__pwn=1>.txt'
open(os.path.join(E.base, HOSTILE), "w").write("x"); open(os.path.join(E.base, "quote\"s & 'amp'.txt"), "w").write("x")
os.makedirs(os.path.join(E.base, "many")); 
for i in range(1200): open(os.path.join(E.base, "many", "photo_%04d.png" % i), "w").close()
open(os.path.join(E.base, "setup.exe"), "w").write("x")
import server, winshell, db as dbmod
calls = []
winshell.open_path = lambda p: (calls.append(("open", p)) or True); winshell.reveal = lambda p: (calls.append(("reveal", p)) or True)
winshell.IS_WIN = False
server.init(database=E.db, settings=E.settings, idx=E.idx); E.idx.watcher_cls = None
E.idx.settings.update({"speed": "fast"}); E.idx._make_rules()
server.on_start()
httpd = server.build_server(PORT); threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/" % PORT
def api(p): return json.loads(urllib.request.urlopen(URL.rstrip("/") + p, timeout=10).read())
t0 = time.time()
while E.idx.state["last_done"] is None and time.time() - t0 < 30: time.sleep(0.1)
dbmod_cloud = E.db.add_entries(E.db.dir_id(E.root), [("cloudy.docx", False, 100, 1700000000, dbmod.FLAG_CLOUD)]); E.db.commit(); E.idx.entry_count += 1
open(os.path.join(E.base, "cloudy.docx"), "w").write("x")
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1320, "height": 860}, permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page(); pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None); pg.on("pageerror", lambda e: errors.append(str(e)))
    dialogs = []; pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    pg.goto(URL); pg.wait_for_selector("#q"); pg.wait_for_timeout(800)
    ok("title + search box focused", pg.title() == "QuickFind" and pg.evaluate("document.activeElement.id") == "q")
    ok("empty query hint with item count", "indexed files and folders" in pg.inner_text("#empty"), repr(pg.inner_text("#empty")) + str(api("/api/status")["index"]["entries"]))
    ok("side status shows count", "items indexed" in pg.inner_text("#side-text"), pg.inner_text("#side-text"))
    pg.fill("#q", "report"); pg.wait_for_timeout(400)
    names = pg.eval_on_selector_all("#rows tr td:first-child", "els => els.map(e => e.textContent)")
    ok("instant results as you type", len(names) == 2 and any("Report Final.pdf" in n for n in names), names)
    ok("matched text highlighted", pg.locator("#rows .hit").first.inner_text().lower() == "report")
    ok("count + ms shown", re.search(r"2 results · [\d.]+ ms", pg.inner_text("#count")) is not None, pg.inner_text("#count"))
    ok("folder + size + date columns", "disk" in pg.inner_text("#rows tr:first-child td:nth-child(2)") and "KB" in pg.inner_text("#rows") )
    pg.fill("#q", "*.pdf"); pg.wait_for_timeout(300); ok("wildcard in UI", pg.locator("#rows tr").count() == 1)
    pg.fill("#q", "IMG_000?.jpg"); pg.wait_for_timeout(300); ok("? wildcard in UI", pg.locator("#rows tr").count() == 2)
    pg.fill("#q", "zzzznothing"); pg.wait_for_timeout(300); ok("no match message", pg.inner_text("#empty") == "No matches." and pg.locator("#rows tr").count() == 0, pg.inner_text("#empty"))
    # filters
    pg.fill("#q", ""); pg.select_option("#f-type", "images"); pg.wait_for_timeout(300)
    ok("type filter with empty query lists matches", pg.locator("#rows tr").count() == 300 and "300+" in pg.inner_text("#count"), pg.inner_text("#count"))
    pg.select_option("#f-type", "any"); pg.fill("#f-ext", "pdf, docx"); pg.wait_for_timeout(300)
    ok("extension filter", sorted(pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)")) == ["Report Final.pdf", "cloudy.docx", "report_draft.docx"], pg.inner_text("#rows"))
    pg.fill("#f-ext", ""); pg.fill("#f-smin", "1"); pg.select_option("#f-smin-u", "1048576"); pg.wait_for_timeout(300)
    ok("min size 1 MB", sorted(pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)")) == ["IMG_0001.JPG", "IMG_0002.jpg", "song.mp3"], pg.inner_text("#rows"))
    pg.fill("#f-smax", "2"); pg.select_option("#f-smax-u", "1048576"); pg.wait_for_timeout(300)
    ok("size range 1-2 MB", sorted(pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)")) == ["IMG_0002.jpg"], pg.inner_text("#rows"))
    pg.click("#f-clear"); pg.wait_for_timeout(300)
    ok("clear filters resets", pg.input_value("#f-smin") == "" and pg.input_value("#f-type") == "any" and "Type a name" in pg.inner_text("#empty"), [pg.input_value("#f-smin"), pg.input_value("#f-type"), pg.inner_text("#empty"), pg.input_value("#q")])
    pg.select_option("#f-date", "7"); pg.wait_for_timeout(300); pg.select_option("#f-type", "files"); pg.wait_for_timeout(300)
    t7 = pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)")
    ok("date filter past 7 days", "notes.txt" in t7 and "IMG_0001.JPG" not in t7 and "song.mp3" not in t7, t7[:5])
    pg.select_option("#f-date", "custom"); ok("custom date inputs appear", pg.is_visible("#f-dfrom"))
    pg.fill("#f-dfrom", "2000-01-01"); pg.fill("#f-dto", "2000-12-31"); pg.wait_for_timeout(300); ok("custom date range (empty era)", pg.locator("#rows tr").count() == 0)
    pg.click("#f-clear"); pg.wait_for_timeout(200)
    # sorting
    pg.fill("#q", "img_000"); pg.wait_for_timeout(300)
    pg.click('button.th[data-sort="size"]'); pg.wait_for_timeout(300)
    ok("click header sorts by size (desc first)", pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)") == ["IMG_0001.JPG", "IMG_0002.jpg"] and pg.get_attribute('button.th[data-sort="size"]', "data-arrow").strip() == "▼")
    ok("aria-sort set", pg.get_attribute('th:has(button[data-sort="size"])', "aria-sort") == "descending")
    pg.click("#f-dir"); pg.wait_for_timeout(300); ok("direction toggle", pg.eval_on_selector_all("#rows tr td:first-child", "e => e.map(x => x.title)") == ["IMG_0002.jpg", "IMG_0001.JPG"])
    pg.select_option("#f-sort", "date"); pg.wait_for_timeout(300); ok("sort select -> date", pg.get_attribute('button.th[data-sort="date"]', "data-arrow").strip() in ("▲", "▼"))
    pg.select_option("#f-sort", "path"); pg.select_option("#f-sort", "name"); pg.click('button.th[data-sort="name"]'); pg.wait_for_timeout(300)
    ok("sort by name toggles", pg.get_attribute('button.th[data-sort="name"]', "data-arrow").strip() in ("▲", "▼"))
    pg.reload(); pg.wait_for_selector("#q"); ok("sort choice remembered", pg.input_value("#f-sort") == "name")
    # keyboard + actions
    if pg.get_attribute('button.th[data-sort="name"]', "data-arrow").strip() == "▼": pg.click("#f-dir")
    pg.fill("#q", "notes"); pg.wait_for_timeout(300)
    ok("actions hidden until a row is selected", pg.is_hidden("#actions"))
    pg.press("#q", "ArrowDown"); ok("arrow selects first", pg.locator("#rows tr.selected").count() == 1 and pg.is_visible("#actions") and pg.inner_text("#sel-path").endswith("notes.txt"))
    pg.fill("#q", "report"); pg.wait_for_timeout(300); pg.press("#q", "ArrowDown"); pg.press("#q", "ArrowDown"); pg.press("#q", "ArrowUp")
    ok("arrows move selection", pg.locator("#rows tr").nth(0).get_attribute("class").endswith("selected"))
    del calls[:]
    pg.press("#q", "Enter"); pg.wait_for_timeout(300); ok("Enter opens selected", calls and calls[-1][0] == "open" and calls[-1][1].endswith("Report Final.pdf"), calls)
    pg.press("#q", "Control+Enter"); pg.wait_for_timeout(300); ok("Ctrl+Enter reveals", calls[-1][0] == "reveal")
    pg.click("#a-open"); pg.wait_for_timeout(200); ok("Open button", calls[-1][0] == "open")
    pg.click("#a-reveal"); pg.wait_for_timeout(200); ok("Show in folder button", calls[-1][0] == "reveal")
    pg.locator("#rows tr").nth(1).dblclick(); pg.wait_for_timeout(300); ok("double-click opens", calls[-1][0] == "open" and calls[-1][1].endswith("report_draft.docx"), calls[-1])
    pg.click("#a-copy"); pg.wait_for_timeout(300)
    clip = pg.evaluate("navigator.clipboard.readText()"); ok("Copy path puts the full path on the clipboard", clip == os.path.join(E.base, "Documents", "report_draft.docx"), clip)
    ok("toast confirms copy", "copied" in pg.inner_text("#toast").lower())
    pg.locator("#rows tr").nth(0).click(); pg.keyboard.press("Control+Shift+C"); pg.wait_for_timeout(300)
    ok("Ctrl+Shift+C copies", pg.evaluate("navigator.clipboard.readText()").endswith("Report Final.pdf") or pg.evaluate("navigator.clipboard.readText()").endswith("report_draft.docx"))
    pg.fill("#q", "setup.exe"); pg.wait_for_timeout(300); pg.press("#q", "ArrowDown"); del calls[:]; pg.press("#q", "Enter"); pg.wait_for_selector(".modal")
    ok("program asks before running", "Run setup.exe" in pg.inner_text(".modal h2") and not calls)
    pg.click(".modal >> text=Cancel"); pg.wait_for_timeout(200); ok("cancel runs nothing, modal closes", not calls and pg.locator(".modal").count() == 0 and pg.evaluate("document.activeElement.id") == "q")
    pg.press("#q", "Enter"); pg.wait_for_selector(".modal"); pg.keyboard.press("Escape"); pg.wait_for_timeout(150); ok("Escape closes confirm", pg.locator(".modal").count() == 0 and not calls)
    pg.press("#q", "Enter"); pg.wait_for_selector(".modal"); pg.click(".modal .danger"); pg.wait_for_timeout(300); ok("confirm runs it", calls and calls[-1][0] == "open")
    pg.fill("#q", "cloudy"); pg.wait_for_timeout(300); ok("online-only tag shown", pg.locator("#rows .tag").count() == 1)
    pg.press("#q", "ArrowDown"); del calls[:]; pg.press("#q", "Enter"); pg.wait_for_selector(".modal")
    ok("online-only asks before download", "Download" in pg.inner_text(".modal h2") and not calls)
    pg.click(".modal .primary"); pg.wait_for_timeout(300); ok("download confirmed", calls and calls[-1][0] == "open")
    # hostile names
    pg.fill("#q", "onerror"); pg.wait_for_timeout(300)
    ok("hostile file name shown as text, not HTML", pg.locator("#rows img").count() == 0 and "<img src=x" in pg.inner_text("#rows") and pg.evaluate("window.__pwn") is None and not dialogs)
    pg.fill("#q", "quote"); pg.wait_for_timeout(300); ok("quotes/ampersands intact", "quote\"s & 'amp'.txt" in pg.inner_text("#rows"))
    pg.fill("#q", '"quote"s'); pg.wait_for_timeout(300); ok("quoted term works", pg.locator("#rows tr").count() == 1)
    # stale item
    os.remove(os.path.join(E.base, "Documents", "notes.txt")); pg.fill("#q", "notes.txt"); pg.wait_for_timeout(300); pg.press("#q", "ArrowDown"); pg.press("#q", "Enter"); pg.wait_for_timeout(500)
    ok("stale item gives a toast", "no longer exists" in pg.inner_text("#toast"), pg.inner_text("#toast"))
    pg.wait_for_timeout(7000); ok("stale item vanishes from list after re-read", pg.locator("#rows tr").count() == 0, pg.inner_text("#empty"))
    # show more
    pg.fill("#q", "photo_"); pg.wait_for_timeout(500)
    ok("first page limited to 300 with '+' count", pg.locator("#rows tr").count() == 300 and "300+" in pg.inner_text("#count") and pg.is_visible("#btn-more"), pg.inner_text("#count"))
    pg.click("#btn-more"); pg.wait_for_timeout(600); ok("show more loads the rest", pg.locator("#rows tr").count() == 1000 and "1,000+" in pg.inner_text("#count") or pg.locator("#rows tr").count() == 1000, pg.locator("#rows tr").count())
    pg.click("#btn-more"); pg.wait_for_timeout(600); ok("show more again reaches all 1200", pg.locator("#rows tr").count() == 1200 and pg.is_hidden("#btn-more"), pg.locator("#rows tr").count())
    # typing fast: only last result sticks
    pg.fill("#q", ""); 
    for ch in "report": pg.keyboard.type(ch, delay=5)
    pg.wait_for_timeout(500); ok("fast typing settles on final query", pg.locator("#rows tr").count() == 2)
    # settings view
    pg.click('[data-view="settings"]'); pg.wait_for_selector("#idx-cards .card"); pg.wait_for_timeout(300)
    ok("index cards", pg.locator("#idx-cards .card").count() == 4 and "items" in pg.inner_text("#idx-cards").lower(), pg.inner_text("#idx-cards"))
    ok("drive list shows root checked", pg.locator("#drive-list input:checked").count() == 1)
    ok("excludes empty state", "No folders skipped" in pg.inner_text("#ex-list"))
    ok("builtin skips listed", "$Recycle.Bin" in pg.inner_text("#builtin"))
    ok("apply disabled until a change", pg.is_disabled("#btn-apply"))
    pg.fill("#ex-add", os.path.join(E.base, "Music")); pg.click("#ex-add-btn"); pg.select_option("#o-speed", "normal"); pg.select_option("#o-rescan", "30"); pg.check("#o-hidden")
    ok("exclude added to list; apply enabled", os.path.join(E.base, "Music") in pg.inner_text("#ex-list") and not pg.is_disabled("#btn-apply"))
    n_err = len(errors); pg.fill("#ex-add", "relative\\path"); pg.click("#ex-add-btn"); pg.click("#btn-apply"); pg.wait_for_timeout(500)
    ok("invalid path rejected with error toast", "full path" in pg.inner_text("#toast"), pg.inner_text("#toast"))
    del errors[n_err:]; pg.click('#ex-list li >> text=relative >> .. >> button'); pg.click("#btn-apply"); pg.wait_for_timeout(800)
    s = api("/api/status")["settings"]; ok("settings applied", s["speed"] == "normal" and s["rescan_min"] == 30 and s["skip_hidden"] is True and s["excludes"] == [os.path.join(E.base, "Music")], s)
    pg.wait_for_timeout(5000); pg.click('[data-view="search"]'); pg.fill("#q", "song.mp3"); pg.wait_for_timeout(400)
    ok("excluded folder gone from results", pg.locator("#rows tr").count() == 0)
    pg.click('[data-view="settings"]'); pg.click('#ex-list li button'); pg.click("#btn-apply"); pg.wait_for_timeout(5000)
    pg.click('[data-view="search"]'); pg.fill("#q", ""); pg.fill("#q", "song.mp3"); pg.wait_for_timeout(500); ok("removing the exclude brings it back", pg.locator("#rows tr").count() == 1)
    pg.click('[data-view="settings"]'); pg.click("#btn-pause"); pg.wait_for_timeout(1500)
    ok("pause button toggles", "Resume" in pg.inner_text("#btn-pause") and "paused" in pg.inner_text("#status").lower())
    pg.click('[data-view="search"]'); ok("paused banner on search view", pg.is_visible("#banner") and "paused" in pg.inner_text("#banner"))
    pg.click('[data-view="settings"]'); pg.click("#btn-pause"); pg.wait_for_timeout(1500); ok("resume works", "Pause" in pg.inner_text("#btn-pause"))
    pg.click("#btn-rescan"); pg.wait_for_timeout(300)
    pg.click("#btn-rebuild"); pg.wait_for_selector(".modal"); ok("rebuild asks first and says files are safe", "none of your files" in pg.inner_text(".modal"))
    pg.click(".modal >> text=Cancel"); pg.wait_for_timeout(200); ok("cancel keeps index", api("/api/status")["index"]["entries"] > 100)
    pg.select_option("#theme", "amber"); ok("theme", pg.evaluate("document.documentElement.dataset.theme") == "amber"); pg.select_option("#theme", "light")
    pg.click('[data-view="search"]'); pg.fill("#q", "report"); pg.wait_for_timeout(300)
    pg.set_viewport_size({"width": 700, "height": 700}); pg.wait_for_timeout(300)
    ok("narrow window: no horizontal page overflow", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]"))
    pg.set_viewport_size({"width": 1320, "height": 860})
    # first-build banner + progressive results on a fresh index
    old = E.settings.get()
    import indexer as _ix
    _orig_sync = _ix.Indexer.sync_dir
    def _slow_sync(self, *a, **k): time.sleep(0.02); return _orig_sync(self, *a, **k)
    _ix.Indexer.sync_dir = _slow_sync
    for i in range(250): os.makedirs(os.path.join(E.base, "bulkdirs", "b%03d" % i), exist_ok=True); open(os.path.join(E.base, "bulkdirs", "b%03d" % i, "bulkfile.txt"), "w").close()
    E.settings.update({"speed": "gentle"}); E.idx._make_rules(); E.idx.submit("reset")
    pg.fill("#q", ""); pg.fill("#q", "bulkfile"); seen_banner = False; counts = []
    for _ in range(120):
        pg.wait_for_timeout(250)
        seen_banner = seen_banner or (pg.is_visible("#banner") and "Building the index" in pg.inner_text("#banner"))
        counts.append(pg.locator("#rows tr").count())
        if E.idx.state["phase"] == "idle" and counts[-1] == 250: break
    ok("first-build banner with progress shown", seen_banner)
    ok("results appear progressively while indexing", len(set(counts)) > 2 and counts[-1] == 250, counts)
    _ix.Indexer.sync_dir = _orig_sync
    pg.wait_for_timeout(1200); ok("banner hidden when done", pg.is_hidden("#banner"))
    ok("no console/page errors", not errors, str(errors[:3]))
    b.close()
httpd.shutdown(); server.on_stop(); E.close()
R.done()
