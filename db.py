"""QuickFind index database (SQLite, WAL). One writer (the indexer thread), one interruptible reader.

Tables
  dirs(id, path, path_lc)      every indexed folder; path always ends with the OS separator
  entries(id, dir_id, name, name_lc, ext, is_dir, size, mtime, flags)   every file and folder name
  fts                          FTS5 trigram index over entries.name_lc (external content) when available
A full path is dirs.path || entries.name, so long folder names are stored once per folder, not per file.
"""
import os
import re
import sqlite3
import threading
import time

SEP = os.sep
SCHEMA_VERSION = "1"
FLAG_HIDDEN, FLAG_SYSTEM, FLAG_CLOUD, FLAG_LINK = 1, 2, 4, 8
HIGH = chr(0x10FFFF)
MAX_LIMIT = 5000

TYPE_EXTS = {
    "documents": "doc docx pdf txt rtf odt xls xlsx xlsm csv ppt pptx odp ods md epub pages numbers",
    "images": "jpg jpeg png gif bmp webp tif tiff heic heif svg ico raw cr2 nef arw dng psd",
    "video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts m2ts",
    "audio": "mp3 wav flac aac ogg m4a wma opus aiff mid midi",
    "archives": "zip rar 7z tar gz tgz bz2 xz iso cab img wim",
    "code": "py js jsx ts tsx java c cpp cc h hpp cs go rs rb php html htm css scss json xml yml yaml toml sh ps1 sql bat cmd lua kt swift",
    "apps": "exe msi msix appx lnk dll sys",
}
TYPE_EXTS = {k: tuple(v.split()) for k, v in TYPE_EXTS.items()}
TYPES = ("any", "files", "folders") + tuple(TYPE_EXTS)
SORTS = ("name", "size", "date", "path")


def split_ext(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if 0 < i < len(name) - 1 and len(name) - i <= 17 else ""


def clean_text(s):
    """SQLite cannot store lone surrogates (undecodable file names); replace them."""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def glob_escape(s):
    return s.replace("[", "[[]")


class QueryError(ValueError):
    pass


TOKEN_RE = re.compile(r'(!?)(?:"([^"]*)"|(\S+))')


def parse_query(q):
    """Split a search string into terms: [(negated, text_lc)]. Quotes keep spaces; a leading ! excludes."""
    terms = []
    for neg, quoted, bare in TOKEN_RE.findall((q or "")[:600]):
        text = (quoted if quoted else bare).strip()
        if not text:
            continue
        if not quoted and bare == "!":
            continue
        terms.append((bool(neg), clean_text(text.lower())[:200]))
        if len(terms) >= 12:
            break
    return terms


def fts_phrase(s):
    return '"' + s.replace('"', '""') + '"'


class Database:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.rlock = threading.Lock()
        self._seq = 0
        self.w = self._connect()
        self._init_schema()
        self.r = self._connect(readonly=False)      # search only (a newer search interrupts it)
        self.u = self._connect()                    # small utility reads (meta, counts, path lookup); never interrupted
        self.ulock = threading.Lock()
        self.next_id = (self.w.execute("SELECT IFNULL(MAX(id),0) FROM entries").fetchone()[0]) + 1
        self.pending = 0
        self.in_tx = False
        self.last_commit = time.monotonic()

    # ---------------------------------------------------------------- setup
    def _connect(self, readonly=False):
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=-20000")       # 20 MB page cache per connection
        c.execute("PRAGMA mmap_size=0")
        c.execute("PRAGMA temp_store=FILE")
        c.execute("PRAGMA wal_autocheckpoint=2000")
        return c

    def _init_schema(self):
        w = self.w
        w.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        row = w.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row and row[0] != SCHEMA_VERSION:
            for t in ("fts", "entries", "dirs"):
                w.execute("DROP TABLE IF EXISTS " + t)
            w.execute("DELETE FROM meta")
        w.executescript("""
            CREATE TABLE IF NOT EXISTS dirs(id INTEGER PRIMARY KEY, path TEXT NOT NULL, path_lc TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, dir_id INTEGER NOT NULL, name TEXT NOT NULL, name_lc TEXT NOT NULL,
                ext TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER, mtime INTEGER, flags INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS entries_dir ON entries(dir_id);
        """)
        have = w.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
        if have is None:
            try:
                w.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name_lc, content='entries', content_rowid='id', tokenize='trigram')")
                have = "1"
            except sqlite3.OperationalError:
                have = "0"
            w.execute("INSERT OR REPLACE INTO meta VALUES('fts', ?)", (have,))
        else:
            have = have[0]
        self.fts = have == "1"
        w.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))

    def close(self):
        for c in (self.w, self.r, self.u):
            try:
                c.close()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------------- meta
    def meta_get(self, key, default=None):
        row = self.w.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_read(self, key, default=None):
        """Same as meta_get but on the reader connection, safe from any thread."""
        with self.ulock:
            row = self.u.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key, value):
        self.w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def meta_del_prefix(self, prefix):
        self.w.execute("DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'", (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))

    # ---------------------------------------------------------------- writes (indexer thread only)
    def begin(self):
        if not self.in_tx:
            self.w.execute("BEGIN")
            self.in_tx = True

    def commit(self):
        if self.in_tx:
            self.w.execute("COMMIT")
            self.in_tx = False
        self.pending = 0
        self.last_commit = time.monotonic()

    def maybe_commit(self, force=False, limit=4000, every=2.0):
        """Commit when a batch is big enough, or every ~2 s so searches see a growing index while it builds."""
        if self.in_tx and (force or self.pending >= limit or time.monotonic() - self.last_commit >= every):
            self.commit()

    def dir_id(self, path, create=True):
        """Row id of a folder (path must end with the separator)."""
        lc = clean_text(path).lower()
        row = self.w.execute("SELECT id FROM dirs WHERE path_lc=?", (lc,)).fetchone()
        if row:
            return row[0]
        if not create:
            return None
        self.begin()
        cur = self.w.execute("INSERT INTO dirs(path, path_lc) VALUES(?,?)", (clean_text(path), lc))
        self.pending += 1
        return cur.lastrowid

    def children(self, dir_id):
        """{name: (id, size, mtime, is_dir, flags)} for one folder."""
        return {r[0]: r[1:] for r in self.w.execute("SELECT name, id, size, mtime, is_dir, flags FROM entries WHERE dir_id=?", (dir_id,))}

    def add_entries(self, dir_id, rows):
        """rows: [(name, is_dir, size, mtime, flags)]. Returns the new ids in the same order."""
        if not rows:
            return []
        self.begin()
        ids, ins, fts = [], [], []
        for name, is_dir, size, mtime, flags in rows:
            i = self.next_id
            self.next_id += 1
            lc = name.lower()
            ids.append(i)
            ins.append((i, dir_id, name, lc, "" if is_dir else split_ext(name), 1 if is_dir else 0, size, mtime, flags))
            fts.append((i, lc))
        self.w.executemany("INSERT INTO entries(id,dir_id,name,name_lc,ext,is_dir,size,mtime,flags) VALUES(?,?,?,?,?,?,?,?,?)", ins)
        if self.fts:
            self.w.executemany("INSERT INTO fts(rowid, name_lc) VALUES(?,?)", fts)
        self.pending += len(rows)
        return ids

    def update_entries(self, rows):
        """rows: [(size, mtime, flags, id)]"""
        if rows:
            self.begin()
            self.w.executemany("UPDATE entries SET size=?, mtime=?, flags=? WHERE id=?", rows)
            self.pending += len(rows)

    def delete_entries(self, ids):
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            qs = ",".join("?" * len(chunk))
            self.begin()
            if self.fts:
                self.w.execute("INSERT INTO fts(fts, rowid, name_lc) SELECT 'delete', id, name_lc FROM entries WHERE id IN (%s)" % qs, chunk)
            self.w.execute("DELETE FROM entries WHERE id IN (%s)" % qs, chunk)
            self.pending += len(chunk)

    def purge_prefix(self, prefix):
        """Remove a folder and everything below it. prefix: folder path ending with the separator."""
        lo = clean_text(prefix).lower()
        hi = lo + HIGH
        self.begin()
        sub = "SELECT id FROM dirs WHERE path_lc>=? AND path_lc<?"
        if self.fts:
            self.w.execute("INSERT INTO fts(fts, rowid, name_lc) SELECT 'delete', id, name_lc FROM entries WHERE dir_id IN (%s)" % sub, (lo, hi))
        cur = self.w.execute("DELETE FROM entries WHERE dir_id IN (%s)" % sub, (lo, hi))
        n = cur.rowcount
        self.w.execute("DELETE FROM dirs WHERE path_lc>=? AND path_lc<?", (lo, hi))
        self.pending += max(n, 1)
        return n

    def find_entry(self, dir_path, name):
        row = self.w.execute("SELECT e.id, e.size, e.mtime, e.is_dir, e.flags FROM entries e JOIN dirs d ON d.id=e.dir_id WHERE d.path_lc=? AND e.name=?",
                             (clean_text(dir_path).lower(), clean_text(name))).fetchone()
        return row

    def reset(self):
        """Delete the whole index (settings and the file itself stay). Returns when the file has been compacted."""
        self.commit()
        self.w.execute("BEGIN")
        if self.fts:
            self.w.execute("INSERT INTO fts(fts) VALUES('delete-all')")
        self.w.execute("DELETE FROM entries")
        self.w.execute("DELETE FROM dirs")
        self.w.execute("DELETE FROM meta WHERE key LIKE 'built:%' OR key LIKE 'count:%'")
        self.w.execute("COMMIT")
        self.next_id = 1
        try:
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.w.execute("VACUUM")
        except sqlite3.Error:
            pass

    def checkpoint(self):
        try:
            self.w.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    # ---------------------------------------------------------------- stats
    def counts(self):
        with self.ulock:
            e = self.u.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            d = self.u.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]
        return e, d

    def dir_count(self):
        with self.ulock:
            return self.u.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]

    def file_bytes(self):
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def path_of(self, entry_id):
        with self.ulock:
            row = self.u.execute("SELECT d.path, e.name, e.is_dir, e.flags, e.ext, d.id FROM entries e JOIN dirs d ON d.id=e.dir_id WHERE e.id=?", (entry_id,)).fetchone()
        if not row:
            return None
        return {"path": row[0] + row[1], "folder": row[0], "name": row[1], "is_dir": bool(row[2]), "flags": row[3], "ext": row[4]}

    # ---------------------------------------------------------------- search
    def build_query(self, spec):
        """spec: dict (q, type, ext, smin, smax, dmin, dmax, sort, dir, limit). Returns (sql, params, highlight_terms)."""
        where, params, narrow, marks = [], [], [], []
        path_expr = "(d.path_lc || e.name_lc)"
        for neg, t in parse_query(spec.get("q")):
            is_path = "/" in t or "\\" in t
            if is_path:
                t = t.replace("/", SEP).replace("\\", SEP)
            wild = "*" in t or "?" in t
            expr = path_expr if is_path else "e.name_lc"
            if not wild:
                cond = "instr(%s, ?) > 0" % expr
                params.append(t)
                if not is_path and not neg:
                    marks.append(t)
            else:
                pat = glob_escape(t)
                if not is_path and not neg:
                    marks.extend(x for x in re.split(r"[*?]+", t) if x)
                if set(t) == {"*"}:
                    continue
                cond = "%s GLOB ?" % expr
                params.append(pat)
            if neg:
                cond = "NOT (%s)" % cond
            else:
                if not is_path and self.fts:
                    narrow.extend(x for x in re.split(r"[*?]+", t) if len(x) >= 3)
            where.append(cond)
        typ = spec.get("type") or "any"
        if typ not in TYPES:
            raise QueryError("unknown type")
        if typ == "files":
            where.append("e.is_dir = 0")
        elif typ == "folders":
            where.append("e.is_dir = 1")
        elif typ in TYPE_EXTS:
            exts = TYPE_EXTS[typ]
            where.append("e.is_dir = 0 AND e.ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
        exts = [x.strip().lstrip(".*").lower() for x in re.split(r"[,\s;]+", spec.get("ext") or "") if x.strip().lstrip(".*")]
        if exts:
            exts = [clean_text(x) for x in exts[:30]]
            where.append("e.is_dir = 0 AND e.ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
        for key, op in (("smin", ">="), ("smax", "<=")):
            v = spec.get(key)
            if v not in (None, ""):
                try:
                    v = int(float(v))
                except (TypeError, ValueError):
                    raise QueryError("size must be a number")
                where.append("e.is_dir = 0 AND e.size %s ?" % op)
                params.append(v)
        for key, op in (("dmin", ">="), ("dmax", "<=")):
            v = spec.get(key)
            if v not in (None, ""):
                try:
                    v = int(float(v))
                except (TypeError, ValueError):
                    raise QueryError("date must be a number")
                where.append("e.mtime %s ?" % op)
                params.append(v)
        sort = spec.get("sort") or "name"
        if sort not in SORTS:
            raise QueryError("unknown sort")
        way = "DESC" if str(spec.get("dir") or "asc").lower() == "desc" else "ASC"
        order = {"name": "e.name_lc %s, d.path_lc" % way, "size": "IFNULL(e.size, -1) %s, e.name_lc" % way,
                 "date": "IFNULL(e.mtime, 0) %s, e.name_lc" % way, "path": "d.path_lc %s, e.name_lc %s" % (way, way)}[sort]
        try:
            limit = max(1, min(MAX_LIMIT, int(spec.get("limit") or 300)))
        except (TypeError, ValueError):
            raise QueryError("limit must be a number")
        if narrow:
            seen, uniq = set(), []
            for x in narrow:
                if x not in seen:
                    seen.add(x)
                    uniq.append(x)
            match = " AND ".join(fts_phrase(clean_text(x)) for x in uniq[:8])
            frm = "fts JOIN entries e ON e.id = fts.rowid JOIN dirs d ON d.id = e.dir_id"
            where.insert(0, "fts MATCH ?")
            params.insert(0, match)
        else:
            frm = "entries e JOIN dirs d ON d.id = e.dir_id"
        sql = "SELECT e.id, e.name, e.is_dir, e.size, e.mtime, e.flags, d.path FROM %s%s ORDER BY %s LIMIT ?" % (
            frm, (" WHERE " + " AND ".join(where)) if where else "", order)
        params.append(limit + 1)
        return sql, params, marks[:8], limit, bool(where) or bool(parse_query(spec.get("q")))

    def search(self, spec):
        """Run a search. A newer search interrupts an older one still running (returns {'cancelled': True})."""
        sql, params, marks, limit, has_cond = self.build_query(spec)
        if not has_cond:        # nothing typed and no filter: do not list the whole disk
            return {"results": [], "more": False, "limit": limit, "ms": 0, "marks": [], "empty_query": True}
        self._seq += 1
        mine = self._seq
        try:
            self.r.interrupt()
        except sqlite3.Error:
            pass
        t0 = time.perf_counter()
        with self.rlock:
            if mine != self._seq:
                return {"cancelled": True}
            try:
                rows = self.r.execute(sql, params).fetchall()
            except sqlite3.OperationalError as e:
                if "interrupt" in str(e).lower():
                    return {"cancelled": True}
                raise
        more = len(rows) > limit
        rows = rows[:limit]
        out = [{"id": r[0], "name": r[1], "is_dir": bool(r[2]), "size": r[3], "mtime": r[4], "flags": r[5], "folder": r[6]} for r in rows]
        return {"results": out, "more": more, "limit": limit, "ms": round((time.perf_counter() - t0) * 1000, 1), "marks": marks, "empty_query": not has_cond}
