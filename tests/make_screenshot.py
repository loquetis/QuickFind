"""Make /workspace/quickfind/screenshot.png: real server + harmless demo tree, headless Chrome 1280x820.
Run: /workspace/_testenv/bin/python tests/make_screenshot.py"""
import json, os, random, sys, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import *
from playwright.sync_api import sync_playwright
PORT = 18779; os.environ["QUICKFIND_PORT"] = str(PORT)
E = Env(tree=False); os.environ["QUICKFIND_DATA"] = E.data
rnd = random.Random(7); now = time.time(); DAY = 86400
def w(rel, size, days):
    p = os.path.join(E.base, *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(b"x")
    t = now - days * DAY - rnd.randint(0, 80000); os.utime(p, (t, t)); SIZES[p] = size
SIZES = {}
# harmless demo tree (made-up names only)
for y, m in [(2025, "Q3"), (2025, "Q4"), (2026, "Q1"), (2026, "Q2"), (2026, "Q3")]:
    w(f"Users/Demo/Documents/Work/Reports/Annual Report {y} {m}.pdf", rnd.randint(300_000, 4_000_000), (2026 - y) * 365 + rnd.randint(5, 200))
w("Users/Demo/Documents/Work/Reports/report-template.docx", 48_000, 12)
w("Users/Demo/Documents/Work/Reports/Sales Report October.xlsx", 212_000, 21)
w("Users/Demo/Documents/Work/Reports/Sales Report November.xlsx", 230_000, 1)
w("Users/Demo/Documents/Work/Budget 2027.xlsx", 96_000, 3)
w("Users/Demo/Documents/Personal/Trip Report - Lisbon.docx", 1_240_000, 160)
w("Users/Demo/Documents/School/Lab Report 3.docx", 380_000, 340)
w("Users/Demo/Downloads/quarterly-report.pdf", 2_100_000, 6)
w("Users/Demo/Downloads/report_final_v2.pdf", 1_800_000, 30)
w("Users/Demo/Downloads/setup-demo-app.exe", 54_000_000, 40)
w("Users/Demo/Projects/web-demo/report.html", 14_000, 9)
w("Users/Demo/Projects/web-demo/src/report.js", 8_800, 9)
w("Users/Demo/Projects/web-demo/src/report.test.js", 3_100, 9)
w("Users/Demo/Projects/data-tools/report_generator.py", 6_400, 55)
w("Users/Demo/Projects/data-tools/tests/test_report_generator.py", 2_900, 55)
w("Users/Demo/Pictures/Vacation/IMG_0412.jpg", 3_400_000, 210)
w("Users/Demo/Pictures/Screenshots/Report layout mockup.png", 640_000, 14)
w("Users/Demo/Music/Podcasts/Weekly Report ep 118.mp3", 41_000_000, 4)
os.makedirs(os.path.join(E.base, "Users/Demo/Documents/Archive/Old Reports"), exist_ok=True)
os.makedirs(os.path.join(E.base, "Users/Demo/Documents/Archive/Report Drafts"), exist_ok=True)
# filler so the index looks lived-in
exts = [("jpg", 2_500_000), ("png", 400_000), ("txt", 3_000), ("pdf", 900_000), ("docx", 60_000), ("mp3", 5_000_000), ("py", 4_000), ("js", 9_000), ("json", 2_000), ("zip", 12_000_000)]
words = "alpha beta gamma delta sigma omega nova atlas orbit pixel cedar maple harbor summit meadow lantern copper river stone".split()
folders = ["Users/Demo/Pictures/2024", "Users/Demo/Pictures/2025", "Users/Demo/Music/Albums", "Users/Demo/Projects/archive", "Users/Demo/Documents/Misc", "Program Files/Demo Suite", "Windows/Logs"]
for i in range(2600):
    e, s = rnd.choice(exts)
    w(f"{rnd.choice(folders)}/{rnd.choice(words)}-{rnd.choice(words)}-{i:04d}.{e}", int(s * rnd.uniform(.2, 2)), rnd.randint(1, 900))
import server, winshell
winshell.IS_WIN = False
server.init(database=E.db, settings=E.settings, idx=E.idx); E.idx.watcher_cls = None
E.idx.settings.update({"speed": "fast"}); E.idx._make_rules()
server.on_start()
t0 = time.time()
while E.idx.state["last_done"] is None and time.time() - t0 < 60: time.sleep(0.1)
# present sizes and Windows-style folder paths for the demo tree (the files on disk are 1-byte stand-ins)
c = E.db.w
base = E.root
for p, s in SIZES.items():
    c.execute("UPDATE entries SET size=? WHERE dir_id=(SELECT id FROM dirs WHERE path=?) AND name=?", (s, os.path.dirname(p) + os.sep, os.path.basename(p)))
for did, path in c.execute("SELECT id, path FROM dirs").fetchall():
    rel = path[len(base):] if path.startswith(base) else path
    newp = "C:\\" + rel.replace("/", "\\")
    c.execute("UPDATE dirs SET path=?, path_lc=? WHERE id=?", (newp, newp.lower(), did))
c.commit()
httpd = server.build_server(PORT); threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/" % PORT
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1280, "height": 820}).new_page()
    pg.goto(URL); pg.wait_for_selector("#q"); pg.wait_for_timeout(600)
    pg.evaluate("localStorage.setItem('theme','dark')") if False else None
    pg.fill("#q", "report"); pg.wait_for_timeout(900)
    pg.mouse.move(1270, 810)
    out = os.path.join(ROOT, "screenshot.png")
    pg.screenshot(path=out); print("saved", out)
    print(pg.inner_text("#count"), "|", pg.inner_text("#side-text"))
    b.close()
httpd.shutdown(); E.close()
