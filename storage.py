"""QuickFind data folder (%APPDATA%\\QuickFind) and the validated settings file."""
import json
import os
import threading

APP = "QuickFind"
DATA_DIR = os.environ.get("QUICKFIND_DATA") or os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.local/share"), APP)
SPEEDS = ("gentle", "normal", "fast")
RESCAN_CHOICES = (0, 15, 30, 60, 180, 720)
MAX_EXCLUDES = 200
BUILTIN_SKIP = ("System Volume Information", "$Recycle.Bin", "$WinREAgent")   # always skipped, any depth


def default_excludes():
    if os.name == "nt":
        return [os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Windows", "WinSxS")]
    return []


def norm_folder(p):
    """Absolute, normalised, no trailing separator (except a drive root). Raises ValueError."""
    if not isinstance(p, str) or not p.strip() or "\x00" in p or len(p) > 1024:
        raise ValueError("not a valid folder path")
    p = p.strip().strip('"')
    if os.name == "nt" and p.startswith("\\\\?\\"):
        p = p[4:]
    if not os.path.isabs(p) or (os.name == "nt" and not (len(p) > 1 and (p[1] == ":" or p.startswith("\\\\")))):
        raise ValueError("use a full path such as C:\\Users\\you\\Folder")
    p = os.path.normpath(p)
    root = os.path.splitdrive(p)[0] + os.sep if os.name == "nt" else os.sep
    if len(p) > len(root):
        p = p.rstrip("\\/")
    return p


def norm_root(p):
    p = norm_folder(p)
    return p if p.endswith(os.sep) else p + os.sep


class Settings:
    DEFAULTS = {"drives_off": [], "skip_system": True, "skip_hidden": False, "speed": "gentle", "rescan_min": 60}

    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "settings.json")
        self.lock = threading.Lock()
        self.data = dict(self.DEFAULTS, excludes=default_excludes())
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = self._clean(json.load(f), self.data)
        except (OSError, ValueError):
            pass

    def _clean(self, raw, base):
        out = dict(base)
        if not isinstance(raw, dict):
            return out
        for key in ("drives_off", "excludes"):
            if key in raw:
                if not isinstance(raw[key], list) or len(raw[key]) > MAX_EXCLUDES:
                    raise ValueError("%s must be a list of at most %d items" % (key, MAX_EXCLUDES))
                seen, items = set(), []
                for v in raw[key]:
                    v = norm_root(v) if key == "drives_off" else norm_folder(v)
                    if v.lower() not in seen:
                        seen.add(v.lower())
                        items.append(v)
                out[key] = items
        for key in ("skip_system", "skip_hidden"):
            if key in raw:
                if not isinstance(raw[key], bool):
                    raise ValueError(key + " must be true or false")
                out[key] = raw[key]
        if "speed" in raw:
            if raw["speed"] not in SPEEDS:
                raise ValueError("speed must be one of: " + ", ".join(SPEEDS))
            out["speed"] = raw["speed"]
        if "rescan_min" in raw:
            if raw["rescan_min"] not in RESCAN_CHOICES or isinstance(raw["rescan_min"], bool):
                raise ValueError("rescan_min must be one of: " + ", ".join(map(str, RESCAN_CHOICES)))
            out["rescan_min"] = raw["rescan_min"]
        return out

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def update(self, raw):
        with self.lock:
            new = self._clean(raw, self.data)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new, f, indent=1)
            os.replace(tmp, self.path)
            self.data = new
            return json.loads(json.dumps(new))
