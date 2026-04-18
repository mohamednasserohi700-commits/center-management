#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
سنتر الدروس الخصوصية - نظام الإدارة
Server: Python HTTP + SQLite database
"""

import os, sys, json, re, sqlite3, hashlib, threading, webbrowser
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote_plus
from datetime import datetime, date, timedelta
import platform
import uuid
import secrets
import base64
import tempfile
from typing import Optional, Tuple, Any, Dict, List

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except Exception:
    Fernet = None  # type: ignore
    _HAS_FERNET = False

# ─── PATHS ───────────────────────────────────────────
# عند PyInstaller: __file__ داخل _MEIPASS (للقراءة)، بينما البيانات تُحفظ بجانب الـ exe
_CODE_ROOT = os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir() -> str:
    env = (os.environ.get("CENTER_DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    # On Railway: use /data volume if available, else /tmp
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        data_dir = "/data"
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _CODE_ROOT


def _pick_resource_dir() -> str:
    env = (os.environ.get("CENTER_RESOURCE_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    return _CODE_ROOT


BASE_DIR = _pick_data_dir()
_RES_DIR = _pick_resource_dir()
_HTML_BUNDLE = os.path.join(_RES_DIR, "app.html")
_HTML_LOCAL = os.path.join(BASE_DIR, "app.html")
HTML_PATH = (
    _HTML_BUNDLE
    if os.path.isfile(_HTML_BUNDLE)
    else (_HTML_LOCAL if os.path.isfile(_HTML_LOCAL) else _HTML_BUNDLE)
)
_DB_CONFIG_FILE = os.path.join(BASE_DIR, "center_db_config.json")
PORT = int(os.environ.get('PORT', 5000))

_REV_COND = threading.Condition()
_DB_REV_MS = 0  # updated by writes + file watcher


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_db_rev_ms() -> int:
    with _REV_COND:
        return int(_DB_REV_MS)


def _bump_db_rev(reason: str = "") -> int:
    global _DB_REV_MS
    with _REV_COND:
        nxt = _now_ms()
        if nxt <= int(_DB_REV_MS):
            nxt = int(_DB_REV_MS) + 1
        _DB_REV_MS = nxt
        _REV_COND.notify_all()
        return nxt


def _db_files_signature(db_path: str) -> Tuple[Tuple[str, int, int], ...]:
    sig: List[Tuple[str, int, int]] = []
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            st = os.stat(p)
            sig.append((p, int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), int(st.st_size)))
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return tuple(sig)


def start_db_file_watcher() -> None:
    def run():
        last: Optional[Tuple[Tuple[str, int, int], ...]] = None
        while True:
            try:
                sig = _db_files_signature(DB_PATH)
                if last is None:
                    last = sig
                elif sig != last:
                    last = sig
                    _bump_db_rev("db_file_changed")
            except Exception:
                pass
            time.sleep(1.0)

    threading.Thread(target=run, daemon=True).start()


def _load_db_path_from_config() -> str:
    default = os.path.join(BASE_DIR, "center.db")
    try:
        if os.path.isfile(_DB_CONFIG_FILE):
            with open(_DB_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                p = (cfg.get("database_file") or cfg.get("path") or "").strip()
                if p:
                    p = os.path.normpath(os.path.expandvars(p))
                    if os.path.isdir(p):
                        p = os.path.join(p, "center.db")
                    return p
    except Exception:
        pass
    return default


DB_PATH = _load_db_path_from_config()

_PRESENCE_LOCK = threading.Lock()
# user_id -> {"name","username","role","last": unix_ts}
_PRESENCE: Dict[int, Dict[str, Any]] = {}
PRESENCE_TTL_SEC = 180

_BACKUP_TABLES_ORDER = [
    "teachers",
    "courses",
    "students",
    "payments",
    "refunds",
    "grades",
    "followups",
    "attendance",
    "expenses",
    "settings",
    "users",
]

# ─── Serial Pool / License ─────────────────────────────
SERIAL_POOL_FILENAME = "center_serial_pool.json"
SERIAL_USED_FILENAME = "center_serial_used.json"
_SERIAL_FILE_ENC_PREFIX = b"CENTER_SERIAL_ENC_V1\n"

DEV_MASTER_USERNAME = "administrator"
DEV_MASTER_DEFAULT_PASSWORD = "3000330210"

# بصمة إصدار للسيرفر (للتأكد إن النسخة الصحيحة شغالة)
SERVER_BUILD = "center-server-refunded-status-revert-guard-v7"

# ─── DATABASE SETUP ──────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _caller_id(body) -> Optional[int]:
    if not body:
        return None
    try:
        v = body.get("caller_id")
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def has_extra_perm(conn, user_id: Optional[int], perm_key: str) -> bool:
    if not user_id:
        return False
    row = conn.execute(
        "SELECT role, perms, active FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row or not row["active"]:
        return False
    if row["role"] in ("admin", "dev_master"):
        return True
    perms = json.loads(row["perms"] or "{}")
    return bool(perms.get(perm_key, 0))


def touch_presence(uid: int, name: str, username: str, role: str) -> None:
    import time

    with _PRESENCE_LOCK:
        _PRESENCE[int(uid)] = {
            "name": name or "",
            "username": username or "",
            "role": role or "",
            "last": time.time(),
        }


def presence_pop(uid: int) -> None:
    with _PRESENCE_LOCK:
        _PRESENCE.pop(int(uid), None)


def build_presence_list(conn, caller_role: str) -> List[dict]:
    import time

    now = time.time()
    with _PRESENCE_LOCK:
        active = {
            int(uid): v
            for uid, v in _PRESENCE.items()
            if now - float(v.get("last", 0)) <= PRESENCE_TTL_SEC
        }
    rows = conn.execute(
        "SELECT id, name, username, role, active FROM users ORDER BY name"
    ).fetchall()
    out: List[dict] = []
    for r in rows:
        if r["role"] == "dev_master" and caller_role != "dev_master":
            continue
        uid = int(r["id"])
        on = uid in active
        out.append(
            {
                "user_id": uid,
                "name": r["name"],
                "username": r["username"],
                "role": r["role"],
                "active_account": bool(r["active"]),
                "online": on,
                "last_seen": (
                    datetime.fromtimestamp(active[uid]["last"]).isoformat(
                        timespec="seconds"
                    )
                    if on
                    else None
                ),
            }
        )
    return out


def save_database_path(new_path: str) -> Tuple[bool, str]:
    """يحدّث مسار قاعدة البيانات ويحفظه في ملف الإعداد."""
    global DB_PATH
    p = os.path.normpath(os.path.expandvars((new_path or "").strip()))
    if not p:
        return False, "مسار فارغ"
    if os.path.isdir(p):
        p = os.path.join(p, "center.db")
    parent = os.path.dirname(os.path.abspath(p))
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return False, f"تعذر إنشاء المجلد الأب تلقائياً: {e}"
    try:
        with open(_DB_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"database_file": p}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, f"تعذر حفظ الإعداد: {e}"
    DB_PATH = p
    return True, p


def _pick_database_dialog(mode: str) -> str:
    """نافذة اختيار ملف أو مجلد (تعمل على سطح المكتب مع Tkinter)."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if mode == "folder":
                p = filedialog.askdirectory(title="اختر المجلد المشترك (سيُستخدم center.db بداخله)")
            else:
                p = filedialog.askopenfilename(
                    title="اختر ملف قاعدة البيانات",
                    filetypes=[
                        ("ملف SQLite", "*.db"),
                        ("كل الملفات", "*.*"),
                    ],
                )
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        return (p or "").strip()
    except Exception:
        return ""


def export_backup_payload(conn, include_dev_master: bool) -> dict:
    c = conn.cursor()
    out: Dict[str, Any] = {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    for table in _BACKUP_TABLES_ORDER:
        rows = c.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = rows_to_list(rows)
    if not include_dev_master:
        out["users"] = [
            u for u in out["users"] if u.get("role") != "dev_master"
        ]
    return out


def import_backup_replace(conn, data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("بيانات غير صالحة")
    c = conn.cursor()
    c.execute("PRAGMA foreign_keys=OFF")
    for table in reversed(_BACKUP_TABLES_ORDER):
        c.execute(f"DELETE FROM {table}")
    try:
        c.execute("DELETE FROM sqlite_sequence")
    except sqlite3.OperationalError:
        pass
    for table in _BACKUP_TABLES_ORDER:
        rows = data.get(table)
        if not isinstance(rows, list) or not rows:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            cols = list(row.keys())
            vals = [row[col] for col in cols]
            ph = ",".join(["?"] * len(cols))
            qcols = ",".join('"' + str(c).replace('"', '""') + '"' for c in cols)
            c.execute(f"INSERT INTO {table} ({qcols}) VALUES ({ph})", vals)
    c.execute("PRAGMA foreign_keys=ON")

# ─── PERMISSION CHECK ────────────────────────────────
# Map of resource → required permission key
RESOURCE_PERMS = {
    "students":   "students",
    "attendance": "attendance",
    "grades":     "grades",
    "followups":  "followup",
    "payments":   "payments",
    "refunds":    "payments",
    "refund":     "payments",
    "student_refund": "payments",
    "expenses":   "expenses",
    "courses":    "courses",
    "teachers":   "teachers",
    "users":      "users",
    "settings":   "settings",
    "billing":    "students",
}
READ_ONLY_RESOURCES = {"dashboard", "reports", "balance", "billing"}  # no perm needed for GET

# صلاحيات كاملة لمطور النظام (تُدمج عند تسجيل الدخول — لا تعتمد على حقل perms الفارغ في قاعدة البيانات)
DEV_MASTER_PERMS_FULL = {
    "dash": 1, "students": 1, "attendance": 1, "sessions": 1, "grades": 1, "followup": 1,
    "payments": 1, "expenses": 1, "reports": 1, "teacher_share": 1, "courses": 1, "teachers": 1,
    "users": 1, "settings": 1, "about": 1, "due": 1, "manual_delete": 1, "serial_licenses": 1,
    "edit_fees": 1, "import_export": 1, "online_users": 1, "shared_database": 1,
}

AR_DAY_NAMES_PY = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]


def _js_weekday_index_py(d: date) -> int:
    """مطابقة JavaScript Date.getDay() حيث 0 = الأحد."""
    return (d.weekday() + 1) % 7


def _course_has_class_on_date_py(days_str: Optional[str], dstr: str) -> bool:
    if not days_str or not str(days_str).strip():
        return True
    try:
        d = datetime.strptime(str(dstr)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    day = AR_DAY_NAMES_PY[_js_weekday_index_py(d)]
    return day in str(days_str)


def _per_session_totals(c, stu: sqlite3.Row) -> Tuple[float, float, int]:
    """للطالب billing_mode=per_session: (إجمالي المستحق، إجمالي المدفوع، عدد الحصص المحسوبة)."""
    sid = int(stu["id"])
    mode = (stu["billing_mode"] or "monthly") or "monthly"
    if mode != "per_session":
        return 0.0, 0.0, 0
    fee = float(stu["fees"] or 0)
    enroll = (stu["date"] or "")[:10] or "1970-01-01"
    course_id = stu["course_id"]
    if not course_id or fee <= 0:
        return 0.0, 0.0, 0
    crow = c.execute("SELECT days FROM courses WHERE id=?", (course_id,)).fetchone()
    days_str = crow["days"] if crow else ""
    rows = c.execute(
        """SELECT date FROM attendance
           WHERE student_id=? AND date>=? AND present=1 ORDER BY date""",
        (sid, enroll),
    ).fetchall()
    cnt = 0
    for r in rows:
        dval = r["date"]
        if dval and _course_has_class_on_date_py(days_str, str(dval)):
            cnt += 1
    paid_total = float(
        c.execute("SELECT COALESCE(SUM(paid),0) FROM payments WHERE student_id=?", (sid,)).fetchone()[0]
    )
    total_due = round(float(cnt) * fee, 2)
    return total_due, paid_total, cnt

def check_perm(conn, token_user_id, resource, is_write=False):
    """Returns (allowed, user_dict) — admin always allowed"""
    if not token_user_id:
        return True, None  # no auth token = public (handled by login)
    row = conn.execute(
        "SELECT role, perms, active FROM users WHERE id=?", (token_user_id,)
    ).fetchone()
    if not row or not row["active"]:
        return False, None
    role = row["role"] or ""
    if role == "admin":
        return True, dict(row)
    if role == "dev_master":
        return True, dict(row)
    perms = json.loads(row["perms"] or "{}")
    perm_key = RESOURCE_PERMS.get(resource)
    if not perm_key:
        return True, dict(row)  # unknown resource — allow
    allowed = bool(perms.get(perm_key, 0))
    return allowed, dict(row)


def _caller_allowed_hard_delete(conn, caller_id) -> bool:
    """حذف سجلات (دفعات، طلاب، …): المدير أو من لديه صلاحية manual_delete. المطور لا يحذف بيانات تشغيلية."""
    if not caller_id:
        return False
    row = conn.execute(
        "SELECT role, perms FROM users WHERE id=? AND active=1", (caller_id,)
    ).fetchone()
    if not row:
        return False
    r = row["role"] or ""
    if r == "admin":
        return True
    if r == "dev_master":
        return True
    perms = json.loads(row["perms"] or "{}")
    return bool(perms.get("manual_delete"))

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'receptionist',
        active INTEGER DEFAULT 1,
        perms TEXT DEFAULT '{}',
        teacher_link_id INTEGER DEFAULT NULL,
        max_opens INTEGER DEFAULT NULL,
        opens_used INTEGER DEFAULT 0,
        trial_message TEXT DEFAULT NULL
    );
    -- add columns if upgrading from older version
    CREATE TABLE IF NOT EXISTS _dummy_users_migration (x INTEGER);

    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        subject TEXT,
        phone TEXT,
        salary REAL DEFAULT 0,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        group_name TEXT NOT NULL,
        teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
        fees REAL DEFAULT 0,
        days TEXT,
        time TEXT,
        description TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT NOT NULL,
        phone TEXT,
        parent_phone TEXT,
        parent_name TEXT,
        grade TEXT,
        course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        fees REAL DEFAULT 0,
        notes TEXT,
        status TEXT DEFAULT 'active',
        date TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
        paid REAL DEFAULT 0,
        required REAL DEFAULT 0,
        date TEXT,
        month TEXT,
        method TEXT DEFAULT 'cash',
        note TEXT,
        status TEXT DEFAULT 'pending',
        by_user TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
        amount REAL NOT NULL,
        date TEXT,
        note TEXT,
        payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
        by_user TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS grades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
        course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        type TEXT,
        score REAL,
        max_score REAL DEFAULT 100,
        date TEXT,
        note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
        type TEXT,
        priority TEXT DEFAULT 'medium',
        note TEXT,
        date TEXT,
        by_user TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
        date TEXT,
        present INTEGER DEFAULT 1,
        note TEXT,
        UNIQUE(student_id, date)
    );

    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        amount REAL DEFAULT 0,
        date TEXT,
        note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Default settings
    defaults = {
        "center_name": "سنتر الدروس الخصوصية",
        "address": "",
        "phone": "",
        "currency": "جنيه",
        "due_day": "5",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Default admin user (password: 1234)
    pw_hash = hashlib.sha256("1234".encode()).hexdigest()
    c.execute("""INSERT OR IGNORE INTO users (name, username, password, role, active, perms)
                 VALUES (?, ?, ?, 'admin', 1, ?)""",
              ("مدير النظام", "admin", pw_hash,
               json.dumps({"dash":1,"students":1,"attendance":1,"grades":1,"followup":1,
                           "payments":1,"expenses":1,"reports":1,"teacher_share":1,"courses":1,
                           "teachers":1,"users":1,"settings":1,"due":1,"about":1,
                           "manual_delete":1,"serial_licenses":0,"edit_fees":1,
                           "import_export":1,"online_users":1,"shared_database":1})))

    # Default dev master (same credentials as law_office.py)
    dev_pw_hash = hashlib.sha256(DEV_MASTER_DEFAULT_PASSWORD.encode()).hexdigest()
    c.execute("""INSERT OR IGNORE INTO users (name, username, password, role, active, perms)
                 VALUES (?, ?, ?, 'dev_master', 1, ?)""",
              ("مطور النظام", DEV_MASTER_USERNAME, dev_pw_hash, json.dumps({})))

    # Sample courses
    c.execute("INSERT OR IGNORE INTO courses (id,name,group_name,fees,days,time) VALUES (1,'رياضيات','مجموعة A',500,'السبت والاثنين','4:00 PM')")
    c.execute("INSERT OR IGNORE INTO courses (id,name,group_name,fees,days,time) VALUES (2,'فيزياء','مجموعة B',450,'الأحد والثلاثاء','5:00 PM')")

    # Migration: add trial columns if not exist
    try:
        c.execute("ALTER TABLE users ADD COLUMN max_opens INTEGER DEFAULT NULL")
    except: pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN opens_used INTEGER DEFAULT 0")
    except: pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN trial_message TEXT DEFAULT NULL")
    except: pass
    # Fix existing trial users with NULL max_opens → default 5
    c.execute("UPDATE users SET max_opens=5 WHERE role='trial' AND max_opens IS NULL")
    c.execute("UPDATE users SET opens_used=0 WHERE role='trial' AND opens_used IS NULL")

    _migrate_schema(c)

    conn.commit()
    conn.close()
    print(f"قاعدة البيانات جاهزة: {DB_PATH}")


def _migrate_schema(c) -> None:
    """أعمدة وجداول إضافية للترقية من نسخ قديمة."""
    try:
        c.execute("ALTER TABLE courses ADD COLUMN session_minutes INTEGER DEFAULT 90")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE teachers ADD COLUMN center_share_type TEXT DEFAULT 'percent'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE teachers ADD COLUMN center_share_value REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute(
            """CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            date TEXT,
            note TEXT,
            payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
            by_user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
        )
    except sqlite3.OperationalError:
        pass
    try:
        c.execute(
            "ALTER TABLE students ADD COLUMN reg_teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL"
        )
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE students ADD COLUMN center_share_type TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE students ADD COLUMN center_share_value REAL")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE students ADD COLUMN billing_mode TEXT DEFAULT 'monthly'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE payments ADD COLUMN receipt_no TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute(
            """UPDATE payments SET status='refunded'
               WHERE (paid IS NULL OR paid<=0.01)
               AND id IN (SELECT payment_id FROM refunds WHERE payment_id IS NOT NULL)"""
        )
    except sqlite3.OperationalError:
        pass
    try:
        for row in c.execute(
            "SELECT id FROM payments WHERE receipt_no IS NULL OR receipt_no=''"
        ).fetchall():
            pid = int(row["id"])
            rno = "RCP-" + str(pid).zfill(8)
            c.execute("UPDATE payments SET receipt_no=? WHERE id=?", (rno, pid))
    except sqlite3.OperationalError:
        pass

# ─── HELPERS ─────────────────────────────────────────
def rows_to_list(rows):
    return [dict(r) for r in rows]

def ok(data=None, msg="success"):
    return {"ok": True, "msg": msg, "data": data}

def err(msg="error"):
    return {"ok": False, "msg": msg}

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ─── Settings helpers ──────────────────────────────────
def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else default)


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, "" if value is None else str(value)),
    )


# ─── Serial Pool / License ─────────────────────────────
_SERIAL_LOCK = threading.Lock()
_SESSION_LICENSE_BYPASS = False


def set_session_license_bypass(active: bool) -> None:
    global _SESSION_LICENSE_BYPASS
    _SESSION_LICENSE_BYPASS = bool(active)


def _app_base_dir() -> str:
    return BASE_DIR


def _serial_pool_path() -> str:
    return os.path.join(_app_base_dir(), SERIAL_POOL_FILENAME)


def _serial_used_path() -> str:
    return os.path.join(_app_base_dir(), SERIAL_USED_FILENAME)


def _get_machine_id() -> str:
    """معرف فريد للجهاز — ثابت على نفس الجهاز."""
    try:
        mac = uuid.getnode()
        hostname = platform.node()
        raw = f"{mac}-{hostname}-CenterSerial2026"
        return hashlib.sha256(raw.encode()).hexdigest()[:32].upper()
    except Exception:
        return "UNKNOWN"


def _format_serial(key: str) -> str:
    k = (key or "")[:32].upper()
    return f"{k[0:4]}-{k[4:8]}-{k[8:12]}-{k[12:16]}-{k[16:20]}-{k[20:24]}-{k[24:28]}-{k[28:32]}"


def _normalize_license_token(s: str) -> str:
    return (s or "").upper().replace(" ", "").replace("-", "").strip()


def _generate_new_pool_serial() -> str:
    return _format_serial(secrets.token_hex(16).upper())


def _pool_kind_to_days(kind: str, custom_days: Optional[int]) -> Tuple[int, bool]:
    k = (kind or "").strip()
    if k == "perpetual":
        return 0, True
    if k == "months_6":
        return 180, False
    if k == "year_1":
        return 365, False
    if k == "days_custom":
        try:
            d = int(custom_days)
            if 1 <= d <= 36500:
                return d, False
        except (TypeError, ValueError):
            pass
        return 365, False
    return 365, False


def _serial_pool_fernet():
    key_mat = hashlib.sha256(b"SystemMakers_CenterSerialPool_FileKey_2026_V1").digest()
    return Fernet(base64.urlsafe_b64encode(key_mat))  # type: ignore[name-defined]


def _read_serial_pool_json(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return default
    if not raw:
        return default

    # encrypted format
    if raw.startswith(_SERIAL_FILE_ENC_PREFIX):
        if not _HAS_FERNET:
            return default
        try:
            body = raw[len(_SERIAL_FILE_ENC_PREFIX) :]
            dec = _serial_pool_fernet().decrypt(body)
            data = json.loads(dec.decode("utf-8"))
            return data if isinstance(data, dict) else default
        except Exception:
            return default

    # plaintext json
    try:
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", prefix="center_serial_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _atomic_write_serial_pool_json(path: str, obj) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    if _HAS_FERNET:
        token = _serial_pool_fernet().encrypt(payload)
        data = _SERIAL_FILE_ENC_PREFIX + token
        _atomic_write_bytes(path, data)
    else:
        _atomic_write_bytes(path, payload)


def _load_serial_pool_entries() -> list:
    data = _read_serial_pool_json(_serial_pool_path(), {})
    ent = data.get("entries")
    return list(ent) if isinstance(ent, list) else []


def _load_serial_used_entries() -> list:
    data = _read_serial_pool_json(_serial_used_path(), {})
    ent = data.get("entries")
    return list(ent) if isinstance(ent, list) else []


def _save_serial_pool_entries(entries: list) -> None:
    _atomic_write_serial_pool_json(_serial_pool_path(), {"version": 1, "entries": entries})


def _save_serial_used_entries(entries: list) -> None:
    _atomic_write_serial_pool_json(_serial_used_path(), {"version": 1, "entries": entries})


def serial_pool_add_entry(kind: str, custom_days: Optional[int] = None):
    """إضافة سريال جديد إلى ملف المخزون."""
    days, perpetual = _pool_kind_to_days(kind, custom_days)
    serial = _generate_new_pool_serial()
    pool = _load_serial_pool_entries()
    pool.append(
        {
            "serial": serial,
            "kind": kind,
            "days": 0 if perpetual else days,
            "perpetual": perpetual,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _save_serial_pool_entries(pool)
    return serial, None


def activate_license_file_pool(conn, serial: str):
    """تفعيل من مخزون السريالات: نقل من pool إلى used وربط الجهاز."""
    serial_norm = _normalize_license_token(serial)
    if not serial_norm:
        return False, "⚠️  أدخل سريال التفعيل."

    machine_id = _get_machine_id()

    with _SERIAL_LOCK:
        pool = _load_serial_pool_entries()
        used = _load_serial_used_entries()

        # إعادة تفعيل نفس السريال (لو كان مستخدم على نفس الجهاز)
        for u in used:
            if _normalize_license_token(u.get("serial", "")) != serial_norm:
                continue
            if (u.get("machine_id") or "") != machine_id:
                return False, "❌  هذا السريال مُستخدم على جهاز آخر ولا يمكن إعادة استخدامه."

            perpetual = bool(u.get("perpetual"))
            exp_s = (u.get("expires_at") or "").strip()[:10]

            set_setting(conn, "license_source", "file_pool")
            set_setting(conn, "license_key", (u.get("serial") or "").strip())
            set_setting(conn, "license_machine", machine_id)
            set_setting(conn, "license_expires_at", "9999-12-31" if perpetual else exp_s)
            set_setting(conn, "license_perpetual", "1" if perpetual else "0")
            set_setting(conn, "license_holder", "")
            set_setting(conn, "license_purchase_date", (u.get("activated_at") or "")[:10])
            set_setting(conn, "license_subscription_days", str(u.get("days") or ""))

            if perpetual:
                return True, "✅  التفعيل مفعّل على هذا الجهاز (اشتراك دائم)."

            try:
                exp_d = date.fromisoformat(exp_s)
            except ValueError:
                return False, "❌  بيانات الصلاحية تالفة."
            if date.today() > exp_d:
                return False, f"❌  انتهت صلاحية الاشتراك ({exp_s})."
            return True, f"✅  التفعيل مفعّل على هذا الجهاز حتى {exp_s}."

        # تفعيل جديد من المخزون
        for e in pool:
            if _normalize_license_token(e.get("serial", "")) != serial_norm:
                continue

            serial_disp = (e.get("serial") or "").strip()
            perpetual = bool(e.get("perpetual"))
            try:
                days = int(e.get("days") or 0)
            except (TypeError, ValueError):
                days = 365

            if perpetual:
                exp_iso = "9999-12-31"
            else:
                exp = date.today() + timedelta(days=max(1, days))
                exp_iso = exp.isoformat()

            new_pool = [
                x
                for x in pool
                if _normalize_license_token(x.get("serial", "")) != serial_norm
            ]
            used.append(
                {
                    "serial": serial_disp,
                    "machine_id": machine_id,
                    "activated_at": datetime.now().isoformat(timespec="seconds"),
                    "expires_at": exp_iso,
                    "perpetual": perpetual,
                    "days": 0 if perpetual else days,
                }
            )

            _save_serial_pool_entries(new_pool)
            _save_serial_used_entries(used)

            set_setting(conn, "license_source", "file_pool")
            set_setting(conn, "license_key", serial_disp)
            set_setting(conn, "license_machine", machine_id)
            set_setting(conn, "license_expires_at", exp_iso)
            set_setting(conn, "license_perpetual", "1" if perpetual else "0")
            set_setting(conn, "license_holder", "")
            set_setting(conn, "license_purchase_date", date.today().isoformat())
            set_setting(conn, "license_subscription_days", "0" if perpetual else str(days))

            set_session_license_bypass(False)

            if perpetual:
                return True, "✅  تم التفعيل! اشتراك دائم على هذا الجهاز."
            return True, f"✅  تم التفعيل! الصلاحية حتى {exp_iso} ({days} يومًا)."

    return False, "❌  السريال غير صحيح أو غير موجود في قائمة السريالات."


def check_license(conn) -> bool:
    """التحقق من صلاحية الترخيص على هذا الجهاز."""
    if _SESSION_LICENSE_BYPASS:
        return True

    saved_key = get_setting(conn, "license_key", "")
    saved_mach = get_setting(conn, "license_machine", "")
    source = (get_setting(conn, "license_source", "") or "").strip()

    if not saved_key:
        return False

    machine_id = _get_machine_id()

    if source == "file_pool":
        if saved_mach and saved_mach != machine_id:
            return False
        if (get_setting(conn, "license_perpetual", "") or "").strip() == "1":
            return True

        exp_s = (get_setting(conn, "license_expires_at", "") or "").strip()
        if not exp_s:
            return False
        try:
            exp_d = date.fromisoformat(exp_s[:10])
        except ValueError:
            return False
        if date.today() > exp_d:
            return False
        return True

    return False


def get_license_status(conn) -> dict:
    active = check_license(conn)
    return {
        "active": bool(active),
        "perpetual": (get_setting(conn, "license_perpetual", "") or "").strip() == "1",
        "expires_at": (get_setting(conn, "license_expires_at", "") or "").strip(),
    }


def next_student_code(conn):
    """كود رقمي يبدأ من 1001 بدون بادئة STU-."""
    rows = conn.execute("SELECT code FROM students").fetchall()
    mx = 1000
    for row in rows:
        cod = (row["code"] or "").strip()
        if not cod:
            continue
        tail = re.sub(r"^STU-?", "", cod, flags=re.I).strip()
        try:
            n = int(tail)
            if n > mx:
                mx = n
        except ValueError:
            continue
    return str(mx + 1)


def _phone_digits(s: str) -> int:
    return len(re.sub(r"\D", "", s or ""))


def _validate_student_phones(phone: str, parent_phone: str) -> Optional[str]:
    if _phone_digits(phone) < 8:
        return "هاتف الطالب إجباري (8 أرقام على الأقل)"
    if _phone_digits(parent_phone) < 8:
        return "هاتف ولي الأمر إجباري (8 أرقام على الأقل)"
    return None


def _opt_positive_int_id(v) -> Optional[int]:
    try:
        if v is None or v is False:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _sync_payment_rows_status(c, stu: sqlite3.Row, month: Optional[str] = None) -> None:
    """بعد تعديل مبلغ دفعة: حدّث حالة الدفعات؛ صفوف «refunded» (مدفوع=٠) تبقى كما هي ولا تُعلَّم مدفوعاً."""
    sid = int(stu["id"])
    bm = (stu["billing_mode"] or "monthly") or "monthly"
    tol = 0.01
    if bm == "per_session":
        total_due, paid_total, _ = _per_session_totals(c, stu)
        rows = c.execute(
            "SELECT id, paid, status FROM payments WHERE student_id=?", (sid,)
        ).fetchall()
        for r in rows:
            p = float(r["paid"] or 0)
            cur = (r["status"] or "").strip().lower()
            if p <= tol and cur == "refunded":
                continue
            if paid_total >= total_due - tol:
                nst = "paid"
            elif paid_total > tol:
                nst = "partial"
            else:
                nst = "pending"
            c.execute("UPDATE payments SET status=? WHERE id=?", (nst, int(r["id"])))
        return
    m = (month or "").strip()
    fees = float(stu["fees"] or 0)
    paid_total = float(
        c.execute(
            "SELECT COALESCE(SUM(paid),0) FROM payments WHERE student_id=? AND month=?",
            (sid, m),
        ).fetchone()[0]
    )
    rows = c.execute(
        "SELECT id, paid, status FROM payments WHERE student_id=? AND month=?",
        (sid, m),
    ).fetchall()
    for r in rows:
        p = float(r["paid"] or 0)
        cur = (r["status"] or "").strip().lower()
        if p <= tol and cur == "refunded":
            continue
        if paid_total >= fees - tol:
            nst = "paid"
        elif paid_total > tol:
            nst = "partial"
        else:
            nst = "pending"
        c.execute("UPDATE payments SET status=? WHERE id=?", (nst, int(r["id"])))


def _apply_refund_to_payment(c, payment_id: int, refund_amount: float) -> None:
    """يخصم مبلغ الاسترجاع من سجل الدفعة المرتبط ويحدّث الحالات."""
    tol = 0.01
    prow = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not prow:
        return
    pay_amt = float(prow["paid"] or 0)
    ref = min(float(refund_amount), pay_amt)
    if ref <= 0:
        return
    new_paid = round(max(0.0, pay_amt - ref), 2)
    sid = int(prow["student_id"])
    month = (prow["month"] or "").strip()
    stu = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not stu:
        return
    if new_paid <= tol:
        c.execute(
            "UPDATE payments SET paid=?, status='refunded' WHERE id=?",
            (new_paid, payment_id),
        )
    else:
        c.execute("UPDATE payments SET paid=? WHERE id=?", (new_paid, payment_id))
    bm = (stu["billing_mode"] or "monthly") or "monthly"
    _sync_payment_rows_status(c, stu, month if bm == "monthly" else None)


def _refund_revert_execute(c, body):
    """حذف سجل الاسترجاع وإرجاع المبلغ لسجل الدفعة المرتبط (إن وُجد)."""
    rid = body.get("id")
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return err("معرّف الاسترجاع غير صالح")
    row = c.execute("SELECT * FROM refunds WHERE id=?", (rid,)).fetchone()
    if not row:
        return err("سجل الاسترجاع غير موجود")
    amt = float(row["amount"] or 0)
    pid = row["payment_id"]
    sid = int(row["student_id"])
    if not pid:
        c.execute("DELETE FROM refunds WHERE id=?", (rid,))
        return ok(msg="تم حذف سجل الاسترجاع")
    prow = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not prow:
        c.execute("DELETE FROM refunds WHERE id=?", (rid,))
        return ok(msg="تم حذف سجل الاسترجاع — الدفعة المرتبطة غير موجودة")
    stu = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not stu:
        return err("الطالب غير موجود")
    tol = 0.01
    bm = (stu["billing_mode"] or "monthly") or "monthly"
    if bm == "monthly":
        mkey = (prow["month"] or "").strip()
        sum_month = float(
            c.execute(
                "SELECT COALESCE(SUM(paid),0) FROM payments WHERE student_id=? AND month=?",
                (sid, mkey),
            ).fetchone()[0]
        )
        fees = float(stu["fees"] or 0)
        if sum_month >= fees - tol:
            return err("عفوا الطالب بالفعل دفع رسوم الشهر ولا يمكن رد المبلغ")
    else:
        total_due, paid_total, _ = _per_session_totals(c, stu)
        if paid_total >= total_due - tol:
            return err("عفوا الطالب بالفعل دفع رسوم الشهر ولا يمكن رد المبلغ")
    new_paid = round(float(prow["paid"] or 0) + amt, 2)
    c.execute("UPDATE payments SET paid=? WHERE id=?", (new_paid, pid))
    month = (prow["month"] or "").strip()
    _sync_payment_rows_status(c, stu, month if bm == "monthly" else None)
    c.execute("DELETE FROM refunds WHERE id=?", (rid,))
    return ok(msg="تم رد المبلغ للدفعة وإزالة سجل الاسترجاع")


def _student_share_override(body) -> Tuple[Optional[str], Optional[float]]:
    """إن لم يُرسل نوع حصة للطالب (أو default/فارغ) نُرجع (None,None) لاستخدام بطاقة المعلم."""
    ct = body.get("center_share_type")
    if ct is None:
        return None, None
    if isinstance(ct, str):
        s = ct.strip().lower()
        if s in ("", "default", "none", "null", "من بطاقة المعلم"):
            return None, None
        ct = s
    else:
        ct = str(ct).strip().lower()
    if ct not in ("percent", "fixed"):
        return None, None
    try:
        v = float(body.get("center_share_value") if body.get("center_share_value") is not None else 0)
    except (TypeError, ValueError):
        v = 0.0
    return ct, v


def _refund_add_execute(c, body):
    """تسجيل استرجاع + خصم المبلغ من سجل الدفعة المرتبط (إن وُجد) لتوافق الإيرادات والمدفوعات."""
    try:
        sid = int(body.get("student_id"))
    except (TypeError, ValueError):
        return err("اختر الطالب وأدخل مبلغ استرجاع صحيح")
    amt = float(body.get("amount") or 0)
    if amt <= 0:
        return err("اختر الطالب وأدخل مبلغ استرجاع صحيح")
    pid_raw = body.get("payment_id")
    pid = None
    if pid_raw not in (None, "", 0, "0"):
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            pid = None
    if pid:
        prow = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not prow:
            return err("دفعة الربط غير موجودة")
        if int(prow["student_id"]) != sid:
            return err("الدفعة لا تخص الطالب المحدد")
        pay_amt = float(prow["paid"] or 0)
        if amt > pay_amt + 0.01:
            return err("مبلغ الاسترجاع أكبر من المدفوع في هذه الدفعة")
    c.execute(
        """INSERT INTO refunds (student_id,amount,date,note,payment_id,by_user)
           VALUES (?,?,?,?,?,?)""",
        (
            sid,
            amt,
            body.get("date", datetime.now().strftime("%Y-%m-%d")),
            body.get("note", ""),
            pid,
            body.get("by", ""),
        ),
    )
    rid = c.lastrowid
    if pid:
        _apply_refund_to_payment(c, pid, amt)
    return ok({"id": rid}, "تم تسجيل الاسترجاع")


def _assign_payment_receipt_no(c, pay_id: int, body) -> Tuple[Optional[str], Optional[str]]:
    """يُحدَّد بعد INSERT. يعيد (رقم_الإيصال, رسالة_خطأ)."""
    inv = (body.get("receipt_no") or body.get("invoice_no") or "").strip()
    if inv:
        dup = c.execute("SELECT id FROM payments WHERE receipt_no=?", (inv,)).fetchone()
        if dup:
            return None, "رقم الفاتورة / الإيصال مستخدم مسبقاً"
        c.execute("UPDATE payments SET receipt_no=? WHERE id=?", (inv, pay_id))
        return inv, None
    rno = datetime.now().strftime("RCP-%Y%m%d-") + str(pay_id).zfill(6)
    c.execute("UPDATE payments SET receipt_no=? WHERE id=?", (rno, pay_id))
    return rno, None


def _parse_center_share_value(body) -> float:
    v = body.get("center_share_value")
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _center_cut_from_payment(paid: float, share_type: str, share_val: float) -> Tuple[float, float]:
    """يعيد (حصة السنتر، حصة المعلم) من مبلغ الدفعة."""
    paid = float(paid or 0)
    st = (share_type or "percent") or "percent"
    sv = float(share_val or 0)
    if paid <= 0:
        return 0.0, 0.0
    if st == "fixed":
        cut = min(max(sv, 0), paid)
    else:
        pct = max(0.0, min(100.0, sv))
        cut = round(paid * (pct / 100.0), 2)
    return cut, round(paid - cut, 2)

# ─── API HANDLERS ────────────────────────────────────
def handle_api(method, path, body, headers):
    # path comes in as /resource/action (after stripping /api prefix)
    parts = [p.strip() for p in path.strip("/").split("/") if p.strip()]
    resource = parts[0].lower() if len(parts) > 0 else ""
    action = parts[1].lower() if len(parts) > 1 else ""

    conn = get_db()
    try:
        result = route(conn, method, resource, action, body, parts)
        # bump revision for successful writes (sync between users)
        try:
            is_write = (
                method == "POST"
                and (
                    resource
                    in (
                        "students",
                        "teachers",
                        "courses",
                        "payments",
                        "refunds",
                        "refund",
                        "student_refund",
                        "grades",
                        "followups",
                        "attendance",
                        "expenses",
                        "users",
                        "settings",
                        "backup",
                        "meta",
                        "license",
                    )
                )
            )
            if is_write and isinstance(result, dict) and result.get("ok"):
                _bump_db_rev(f"{resource}/{action}")
        except Exception:
            pass
        conn.commit()
    except Exception as e:
        conn.rollback()
        result = err(str(e))
    finally:
        conn.close()
    return result

def route(conn, method, resource, action, body, parts):
    c = conn.cursor()
    # تطبيع المسار لدعم الصيغ مثل /presence/ أو //backup/export
    n_parts = [str(x).strip() for x in (parts or []) if str(x).strip()]
    resource = (n_parts[0] if len(n_parts) > 0 else (resource or "")).strip().lower()
    action_raw = n_parts[1] if len(n_parts) > 1 else (action or "")
    action = str(action_raw).strip().lower()
    # parts[0]=resource, parts[1]=action, parts[2]=extra param
    extra = n_parts[2] if len(n_parts) > 2 else ""

    # ── License gate (بعد تسجيل الدخول) ─────────────────
    # نمنع الوصول لباقي الصفحات/العمليات بدون تفعيل السريال.
    # المطور dev_master فقط يُسمح له بالتجربة والإدارة.
    if resource not in ("auth", "license"):
        caller_id = body.get("caller_id")
        if caller_id:
            caller_row = conn.execute(
                "SELECT role, active FROM users WHERE id=?",
                (caller_id,),
            ).fetchone()
            caller_role = (caller_row["role"] if caller_row and caller_row["active"] else "")
            if caller_role != "dev_master":
                if not check_license(conn):
                    return err("⚠️  يتطلب تفعيل السريال قبل استخدام النظام. الرجاء من فضلك تفعيل الترخيص أولاً.")
        else:
            # في GET عادةً لا يرسل caller_id إلا بعد تعديل واجهة السنتر
            return err("⚠️  يرجى تسجيل الدخول وتفعيل السريال قبل استخدام النظام.")

    # ── فحص الصلاحيات لعمليات الكتابة ──
    is_write = method == "POST" and action in (
        "add",
        "update",
        "delete",
        "save",
        "add_payment",
        "refund",
        "refund_save",
        "revert",
    )
    if is_write and resource not in ("auth",):
        caller_id = body.get("caller_id")  # كل طلبات الكتابة ترسل caller_id
        if caller_id:
            allowed, caller = check_perm(conn, caller_id, resource, is_write=True)
            if not allowed:
                perm_key = RESOURCE_PERMS.get(resource, resource)
                return err(f"ليس لديك صلاحية لـ {perm_key} — تواصل مع المدير")

    # ── استرجاع مبالغ — عدة مسارات متوافقة (تفادي «مسار غير موجود» بين النسخ)
    if method == "POST" and resource == "student_refund" and action == "save":
        return _refund_add_execute(c, body)
    if method == "POST" and resource == "payments" and action == "refund_save":
        return _refund_add_execute(c, body)

    # ── AUTH ──
    if resource == "auth":
        if action == "login":
            u = body.get("username","")
            p = hash_pw(body.get("password",""))
            row = c.execute("SELECT * FROM users WHERE username=? AND password=? AND active=1",(u,p)).fetchone()
            if not row: return err("بيانات غير صحيحة")
            user = dict(row)
            user["perms"] = json.loads(user.get("perms") or "{}")
            if user.get("role") == "dev_master":
                user["perms"] = dict(DEV_MASTER_PERMS_FULL)

            # ── فحص الحساب التجريبي ──
            if user.get("role") == "trial":
                uid = user["id"]

                # اقرأ القيم مباشرة من DB بشكل نظيف (تجنب NULL)
                fresh = c.execute(
                    "SELECT opens_used, max_opens, trial_message FROM users WHERE id=?", (uid,)
                ).fetchone()

                raw_max  = fresh["max_opens"]
                raw_used = fresh["opens_used"]
                msg      = fresh["trial_message"] or "انتهت فترة التجربة — يرجى التواصل مع المطور للاشتراك"

                # لو max_opens لم يُحدَّد بعد في DB — اعتبره 5 واحفظه
                if raw_max is None:
                    raw_max = 5
                    c.execute("UPDATE users SET max_opens=5 WHERE id=?", (uid,))
                    conn.commit()  # حفظ الـ default فوراً

                max_o = int(raw_max)
                used  = int(raw_used or 0)

                # ── هل تجاوز الحد؟ رفض فوري ──
                if used >= max_o:
                    conn.commit()
                    return err(json.dumps({
                        "trial_expired": True,
                        "trial_message": msg,
                        "opens_used":    used,
                        "max_opens":     max_o,
                    }))

                # ── مسموح: زيّد العداد واحفظ فوراً ──
                c.execute("UPDATE users SET opens_used = opens_used + 1 WHERE id=?", (uid,))
                conn.commit()

                # أعد قراءة القيمة المحدثة
                updated = c.execute("SELECT opens_used FROM users WHERE id=?", (uid,)).fetchone()
                user["opens_used"]    = int(updated["opens_used"])
                user["max_opens"]     = max_o
                user["trial_message"] = msg

            # ── License status for frontend gating ──
            ls = get_license_status(conn)
            if user.get("role") == "dev_master":
                ls["active"] = True  # developer can access serial pages regardless
            user["license_active"] = bool(ls.get("active"))
            user["license_expires_at"] = ls.get("expires_at") or ""
            user["license_perpetual"] = bool(ls.get("perpetual"))

            touch_presence(
                int(user["id"]),
                user.get("name") or "",
                user.get("username") or "",
                user.get("role") or "",
            )
            return ok(user)
        if action == "change_password":
            uid = body["user_id"]; old = hash_pw(body["old"]); new = hash_pw(body["new"])
            row = c.execute("SELECT id FROM users WHERE id=? AND password=?",(uid,old)).fetchone()
            if not row: return err("كلمة المرور الحالية غير صحيحة")
            c.execute("UPDATE users SET password=? WHERE id=?",(new,uid))
            return ok(msg="تم تغيير كلمة المرور")

    # ── LICENSE (Serial Pool) ─────────────────────────────
    if resource == "license":
        caller_id = body.get("caller_id")

        # status (public: only returns for active logged in if caller_id exists)
        if action == "status" and method == "GET":
            status = get_license_status(conn)
            return ok(status, "success")

        # activate serial (for all logged-in users)
        if action == "activate" and method == "POST":
            serial = (body.get("serial") or "").strip()
            okk, msg = activate_license_file_pool(conn, serial)
            # return status too
            status = get_license_status(conn)
            return {"ok": bool(okk), "msg": msg, "data": status}

        # dev endpoints
        if action in ("pool", "used") and method == "GET":
            # for pool/used: GET endpoints
            if not caller_id:
                return err("غير مصرح — يرجى تسجيل الدخول")
            caller_row = c.execute("SELECT role, active FROM users WHERE id=?", (caller_id,)).fetchone()
            if not caller_row or not caller_row["active"] or caller_row["role"] != "dev_master":
                return err("غير مصرح — فقط المطور")
            if action == "pool":
                return ok(_load_serial_pool_entries(), "success")
            if action == "used":
                return ok(_load_serial_used_entries(), "success")

        if action == "pool_add" and method == "POST":
            if not caller_id:
                return err("غير مصرح — يرجى تسجيل الدخول")
            caller_row = c.execute("SELECT role, active FROM users WHERE id=?", (caller_id,)).fetchone()
            if not caller_row or not caller_row["active"] or caller_row["role"] != "dev_master":
                return err("غير مصرح — فقط المطور")
            kind = (body.get("kind") or "").strip()
            custom_days = body.get("custom_days", None)
            if custom_days is not None:
                try:
                    custom_days = int(custom_days)
                except Exception:
                    custom_days = None
            serial, lerr = serial_pool_add_entry(kind, custom_days)
            if lerr:
                return err(lerr)
            return ok({"serial": serial}, "success")

        return err("endpoint license غير معروف")

    # ── META ──
    if resource == "meta":
        if action == "version":
            return ok(
                {
                    "build": SERVER_BUILD,
                    "base_dir": BASE_DIR,
                    "db": DB_PATH,
                    "db_config_file": _DB_CONFIG_FILE,
                },
                "success",
            )
        if action == "rev" and method == "GET":
            return ok({"rev": _get_db_rev_ms()}, "success")
        if action == "wait_rev" and method == "GET":
            try:
                since = int(body.get("since") or 0)
            except Exception:
                since = 0
            deadline = time.time() + 25.0
            with _REV_COND:
                while int(_DB_REV_MS) <= since and time.time() < deadline:
                    _REV_COND.wait(timeout=1.0)
                return ok({"rev": int(_DB_REV_MS)}, "success")
        if action == "db_config" and method == "GET":
            cid = _caller_id(body)
            if not has_extra_perm(conn, cid, "shared_database"):
                return err("ليس لديك صلاحية عرض/تعديل مسار قاعدة البيانات")
            return ok(
                {
                    "database_file": DB_PATH,
                    "config_file": _DB_CONFIG_FILE,
                },
                "success",
            )
        if action == "set_database" and method == "POST":
            cid = _caller_id(body)
            if not has_extra_perm(conn, cid, "shared_database"):
                return err("ليس لديك صلاحية تعديل مسار قاعدة البيانات")
            path_in = (body.get("path") or body.get("database_file") or "").strip()
            okk, res = save_database_path(path_in)
            if not okk:
                return err(res)
            # تهيئة الجداول في الملف الجديد إن لزم
            try:
                init_db()
            except Exception as e:
                return err(f"تم حفظ المسار لكن فشلت تهيئة القاعدة: {e}")
            return ok({"database_file": res}, "تم تغيير مسار قاعدة البيانات — أعد تحميل الصفحة")

        if action == "pick_database" and method == "POST":
            cid = _caller_id(body)
            if not has_extra_perm(conn, cid, "shared_database"):
                return err("ليس لديك صلاحية تعديل مسار قاعدة البيانات")
            mode = (body.get("mode") or "file").strip()
            if mode not in ("file", "folder"):
                mode = "file"
            picked = _pick_database_dialog(mode)
            if not picked:
                return err("لم يُحدد مسار — ألغيت الاختيار أو تعذر فتح النافذة على هذا الجهاز")
            return ok({"path": picked}, "success")

    # ── PRESENCE (متصلون بالبرنامج) ──
    if resource == "presence":
        cid = _caller_id(body)
        if action == "ping" and method == "POST":
            if not cid:
                return err("غير مصرح")
            ur = c.execute(
                "SELECT name, username, role, active FROM users WHERE id=?", (cid,)
            ).fetchone()
            if not ur or not ur["active"]:
                return err("مستخدم غير صالح")
            touch_presence(cid, ur["name"], ur["username"], ur["role"])
            return ok(msg="ok")
        if action == "logout" and method == "POST":
            if cid:
                presence_pop(cid)
            return ok(msg="ok")
        if method == "GET" or (method == "POST" and not action):
            if not has_extra_perm(conn, cid, "online_users"):
                return err("ليس لديك صلاحية عرض شاشة المتصلين")
            cr = ""
            if cid:
                rr = c.execute("SELECT role FROM users WHERE id=?", (cid,)).fetchone()
                if rr:
                    cr = rr["role"] or ""
            return ok(build_presence_list(conn, cr), "success")

    # ── BACKUP استيراد/تصدير ──
    if resource == "backup":
        cid = _caller_id(body)
        if not has_extra_perm(conn, cid, "import_export"):
            return err("ليس لديك صلاحية الاستيراد والتصدير")
        include_dev = False
        if cid:
            rr = c.execute("SELECT role FROM users WHERE id=?", (cid,)).fetchone()
            if rr and rr["role"] == "dev_master":
                include_dev = True
        if action == "export" and method in ("GET", "POST"):
            payload = export_backup_payload(conn, include_dev)
            return ok(payload, "success")
        if action == "import" and method == "POST":
            data = body.get("data")
            if not isinstance(data, dict):
                return err("أرسل حقل data ككائن JSON من ملف التصدير")
            import_backup_replace(conn, data)
            return ok(msg="تم الاستيراد — أعد تحميل البيانات")

    # ── SETTINGS ──
    if resource == "settings":
        if method == "GET":
            rows = c.execute("SELECT key,value FROM settings").fetchall()
            return ok({r["key"]:r["value"] for r in rows})
        if method == "POST":
            for k, v in body.items():
                if k == "caller_id":
                    continue
                c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, str(v)))
            return ok(msg="تم حفظ الإعدادات")

    # ── TEACHERS ──
    if resource == "teachers":
        caller_id = body.get("caller_id")
        role = ""
        if caller_id:
            ur = c.execute("SELECT role, active FROM users WHERE id=?", (caller_id,)).fetchone()
            if ur and ur["active"]:
                role = ur["role"] or ""
        # المدرس ممنوع رؤية/إضافة/تعديل/حذف المدرسين
        if role == "teacher":
            return err("🚫 غير مصرح: لا يمكن للمدرس الوصول إلى صفحة المدرسين")
        if method == "GET":
            return ok(rows_to_list(c.execute("SELECT * FROM teachers ORDER BY name").fetchall()))
        if method == "POST":
            if action == "add":
                c.execute(
                    """INSERT INTO teachers (name,subject,phone,salary,notes,center_share_type,center_share_value)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        body["name"],
                        body.get("subject", ""),
                        body.get("phone", ""),
                        body.get("salary", 0),
                        body.get("notes", ""),
                        body.get("center_share_type") or "percent",
                        _parse_center_share_value(body),
                    ),
                )
                return ok({"id": c.lastrowid}, "تم إضافة المدرس")
            if action == "update":
                c.execute(
                    """UPDATE teachers SET name=?,subject=?,phone=?,salary=?,notes=?,
                       center_share_type=?,center_share_value=? WHERE id=?""",
                    (
                        body["name"],
                        body.get("subject", ""),
                        body.get("phone", ""),
                        body.get("salary", 0),
                        body.get("notes", ""),
                        body.get("center_share_type") or "percent",
                        _parse_center_share_value(body),
                        body["id"],
                    ),
                )
                return ok(msg="تم التعديل")
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, caller_id):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM teachers WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── COURSES ──
    if resource == "courses":
        if method == "GET":
            caller_id = body.get("caller_id")
            role = ""
            tlink = None
            if caller_id:
                ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                if ur and ur["active"]:
                    role = ur["role"] or ""
                    tlink = ur["teacher_link_id"]
            base_sql = """SELECT c.*, t.name as teacher_name
                FROM courses c LEFT JOIN teachers t ON c.teacher_id=t.id
            """
            if role == "teacher":
                if not tlink:
                    return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح من إدارة المستخدمين")
                rows = c.execute(base_sql + " WHERE c.teacher_id=? ORDER BY c.name", (tlink,)).fetchall()
                return ok(rows_to_list(rows))
            rows = c.execute(base_sql + " ORDER BY c.name").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "add":
                caller_id = body.get("caller_id")
                role = ""
                tlink = None
                if caller_id:
                    ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                    if ur and ur["active"]:
                        role = ur["role"] or ""
                        tlink = ur["teacher_link_id"]
                # المدرس يُسمح له بإضافة مجموعته فقط (ويُفرض teacher_id = teacher_link_id)
                if role == "teacher":
                    if not tlink:
                        return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح من إدارة المستخدمين")
                    body["teacher_id"] = tlink
                c.execute(
                    """INSERT INTO courses (name,group_name,teacher_id,fees,days,time,description,session_minutes)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        body["name"],
                        body["group_name"],
                        body.get("teacher_id") or None,
                        body.get("fees", 0),
                        body.get("days", ""),
                        body.get("time", ""),
                        body.get("description", ""),
                        int(body.get("session_minutes") or 90),
                    ),
                )
                return ok({"id": c.lastrowid}, "تم إضافة المجموعة")
            if action == "update":
                caller_id = body.get("caller_id")
                role = ""
                tlink = None
                if caller_id:
                    ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                    if ur and ur["active"]:
                        role = ur["role"] or ""
                        tlink = ur["teacher_link_id"]
                if role == "teacher":
                    if not tlink:
                        return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح من إدارة المستخدمين")
                    row = c.execute("SELECT teacher_id FROM courses WHERE id=?", (body["id"],)).fetchone()
                    if not row:
                        return err("المجموعة غير موجودة")
                    if row["teacher_id"] != tlink:
                        return err("🚫 لا يمكنك تعديل مجموعة ليست تابعة لك")
                    # فرض بقاء teacher_id الخاص بالمجموعة
                    body["teacher_id"] = tlink
                c.execute(
                    """UPDATE courses SET name=?,group_name=?,teacher_id=?,fees=?,days=?,time=?,description=?,
                       session_minutes=? WHERE id=?""",
                    (
                        body["name"],
                        body["group_name"],
                        body.get("teacher_id") or None,
                        body.get("fees", 0),
                        body.get("days", ""),
                        body.get("time", ""),
                        body.get("description", ""),
                        int(body.get("session_minutes") or 90),
                        body["id"],
                    ),
                )
                return ok(msg="تم التعديل")
            if action == "delete":
                caller_id = body.get("caller_id")
                role = ""
                tlink = None
                if caller_id:
                    ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                    if ur and ur["active"]:
                        role = ur["role"] or ""
                        tlink = ur["teacher_link_id"]
                if role == "teacher":
                    if not tlink:
                        return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح من إدارة المستخدمين")
                    row = c.execute("SELECT teacher_id FROM courses WHERE id=?", (body["id"],)).fetchone()
                    if not row:
                        return err("المجموعة غير موجودة")
                    if row["teacher_id"] != tlink:
                        return err("🚫 لا يمكنك حذف مجموعة ليست تابعة لك")
                    c.execute("DELETE FROM courses WHERE id=?", (body["id"],))
                    return ok(msg="تم الحذف")
                if not _caller_allowed_hard_delete(conn, caller_id):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM courses WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── STUDENTS ──
    if resource == "students":
        if method == "GET":
            caller_id = body.get("caller_id")
            role = ""
            tlink = None
            if caller_id:
                ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                if ur and ur["active"]:
                    role = ur["role"] or ""
                    tlink = ur["teacher_link_id"]
            # أعمدة الطالب صراحةً (بدون s.*) حتى لا يُستبدل center_share_* أو غيره بسبب تكرار أسماء الأعمدة في sqlite
            base_sql = """SELECT s.id, s.code, s.name, s.phone, s.parent_phone, s.parent_name, s.grade, s.course_id,
                s.fees, s.notes, s.status, s.date, s.created_at, s.reg_teacher_id,
                s.center_share_type, s.center_share_value, s.billing_mode,
                c.name AS course_name, c.group_name AS group_name, c.fees AS course_fees,
                t.name AS teacher_name, t.id AS teacher_id,
                tr.name AS reg_teacher_name
                FROM students s
                LEFT JOIN courses c ON s.course_id=c.id
                LEFT JOIN teachers t ON c.teacher_id=t.id
                LEFT JOIN teachers tr ON s.reg_teacher_id=tr.id
            """
            if role == "teacher":
                if not tlink:
                    return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح من إدارة المستخدمين")
                rows = c.execute(
                    base_sql + " WHERE (c.teacher_id=? OR s.reg_teacher_id=?) ORDER BY s.id DESC",
                    (tlink, tlink),
                ).fetchall()
                return ok(rows_to_list(rows))
            rows = c.execute(base_sql + " ORDER BY s.id DESC").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            caller_id = body.get("caller_id")
            role = ""
            tlink = None
            if caller_id:
                ur = c.execute("SELECT role, teacher_link_id, active FROM users WHERE id=?", (caller_id,)).fetchone()
                if ur and ur["active"]:
                    role = ur["role"] or ""
                    tlink = ur["teacher_link_id"]

            if action == "add":
                if role == "teacher":
                    if not tlink:
                        return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح")
                    cid = body.get("course_id")
                    if not cid:
                        return err("⚠️ اختر مجموعة للطالب")
                    ok_course = c.execute("SELECT id FROM courses WHERE id=? AND teacher_id=?", (cid, tlink)).fetchone()
                    if not ok_course:
                        return err("🚫 لا يمكنك إضافة طالب لمجموعة ليست تابعة لك")
                    reg_tid_chk = _opt_positive_int_id(body.get("reg_teacher_id"))
                    if reg_tid_chk and int(reg_tid_chk) != int(tlink):
                        return err("🚫 معلم التسجيل يجب أن يكون نفس حسابك")
                pe = _validate_student_phones(body.get("phone", ""), body.get("parent_phone", ""))
                if pe:
                    return err(pe)
                code = next_student_code(conn)
                reg_tid = _opt_positive_int_id(body.get("reg_teacher_id"))
                sh_t, sh_v = _student_share_override(body)
                bm = body.get("billing_mode") or "monthly"
                if bm not in ("monthly", "per_session"):
                    bm = "monthly"
                c.execute(
                    """INSERT INTO students (code,name,phone,parent_phone,parent_name,grade,course_id,fees,notes,status,date,
                       reg_teacher_id,center_share_type,center_share_value,billing_mode)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        code,
                        body["name"],
                        body.get("phone", ""),
                        body.get("parent_phone", ""),
                        body.get("parent_name", ""),
                        body.get("grade", ""),
                        body.get("course_id") or None,
                        body.get("fees", 0),
                        body.get("notes", ""),
                        body.get("status", "active"),
                        body.get("date", datetime.now().strftime("%Y-%m-%d")),
                        reg_tid,
                        sh_t,
                        sh_v,
                        bm,
                    ),
                )
                return ok(
                    {
                        "id": c.lastrowid,
                        "code": code,
                        "center_share_type": sh_t,
                        "center_share_value": sh_v,
                    },
                    "تم إضافة الطالب",
                )
            if action == "update":
                if role == "teacher":
                    if not tlink:
                        return err("⚠️ المعلم غير مربوط بمدرس — يرجى الإصلاح")
                    sid = body.get("id")
                    row = c.execute(
                        """SELECT s.id, c.teacher_id, s.reg_teacher_id FROM students s
                        LEFT JOIN courses c ON s.course_id=c.id WHERE s.id=?""",
                        (sid,),
                    ).fetchone()
                    if not row:
                        return err("الطالب غير موجود")
                    ctid = row["teacher_id"]
                    rtid = row["reg_teacher_id"]
                    if int(ctid or 0) != int(tlink) and int(rtid or 0) != int(tlink):
                        return err("🚫 لا يمكنك تعديل طالب ليس ضمن مجموعاتك")
                    cid = body.get("course_id")
                    if cid:
                        ok_course = c.execute("SELECT id FROM courses WHERE id=? AND teacher_id=?", (cid, tlink)).fetchone()
                        if not ok_course:
                            return err("🚫 لا يمكنك نقل الطالب لمجموعة ليست تابعة لك")
                    reg_tid_chk = _opt_positive_int_id(body.get("reg_teacher_id"))
                    if reg_tid_chk and int(reg_tid_chk) != int(tlink):
                        return err("🚫 معلم التسجيل يجب أن يكون نفس حسابك")
                pe = _validate_student_phones(body.get("phone", ""), body.get("parent_phone", ""))
                if pe:
                    return err(pe)
                reg_tid = _opt_positive_int_id(body.get("reg_teacher_id"))
                sh_t, sh_v = _student_share_override(body)
                bm = body.get("billing_mode") or "monthly"
                if bm not in ("monthly", "per_session"):
                    bm = "monthly"
                c.execute(
                    """UPDATE students SET name=?,phone=?,parent_phone=?,parent_name=?,grade=?,
                             course_id=?,fees=?,notes=?,status=?,date=?,
                             reg_teacher_id=?,center_share_type=?,center_share_value=?,billing_mode=? WHERE id=?""",
                    (
                        body["name"],
                        body.get("phone", ""),
                        body.get("parent_phone", ""),
                        body.get("parent_name", ""),
                        body.get("grade", ""),
                        body.get("course_id") or None,
                        body.get("fees", 0),
                        body.get("notes", ""),
                        body.get("status", "active"),
                        body.get("date", ""),
                        reg_tid,
                        sh_t,
                        sh_v,
                        bm,
                        body["id"],
                    ),
                )
                return ok(
                    {
                        "id": body["id"],
                        "center_share_type": sh_t,
                        "center_share_value": sh_v,
                    },
                    "تم التعديل",
                )
            if action == "delete":
                if role == "teacher":
                    return err("🚫 لا يمكن للمدرس حذف الطلاب — تواصل مع المدير")
                if not _caller_allowed_hard_delete(conn, caller_id):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM students WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── PAYMENTS ──
    if resource == "payments":
        if method == "GET":
            rows = c.execute("""SELECT p.*, s.name as student_name, s.code as student_code,
                c.name as course_name, c.group_name
                FROM payments p
                LEFT JOIN students s ON p.student_id=s.id
                LEFT JOIN courses c ON s.course_id=c.id
                ORDER BY p.id DESC""").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "add":
                paid     = float(body.get("paid",0))
                req      = float(body.get("required",0))
                month    = body.get("month", datetime.now().strftime("%Y-%m"))
                sid      = body["student_id"]
                stu_row = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
                if not stu_row:
                    return err("الطالب غير موجود")
                bm = (stu_row["billing_mode"] or "monthly") or "monthly"
                if bm == "per_session":
                    total_due, paid_total, _sc = _per_session_totals(c, stu_row)
                    remaining = max(0.0, total_due - paid_total)
                    if remaining <= 0:
                        return err("لا يوجد مبلغ مستحق — الطالب محاسب «بالحصة» وسدّد المستحق حتى الآن")
                    if paid > remaining + 0.01:
                        return err(f"المبلغ المدفوع ({paid}) أكبر من المتبقي ({remaining:.2f})")
                    ps_month = "__per_session__"
                    new_paid_total = paid_total + paid
                    status = "paid" if new_paid_total >= total_due - 0.01 else ("partial" if paid > 0 else "pending")
                    req_send = min(req, remaining) if req > 0 else remaining
                    c.execute("""INSERT INTO payments (student_id,paid,required,date,month,method,note,status,by_user)
                                 VALUES (?,?,?,?,?,?,?,?,?)""",
                              (sid, paid, req_send,
                               body.get("date", datetime.now().strftime("%Y-%m-%d")),
                               ps_month,
                               body.get("method","cash"), body.get("note",""), status, body.get("by","")))
                    pid = c.lastrowid
                    rno, rerr = _assign_payment_receipt_no(c, pid, body)
                    if rerr:
                        return err(rerr)
                    return ok({"id": pid, "status": status, "receipt_no": rno}, "تم تسجيل الدفعة")
                # ── شهري: التحقق من دفع الشهر ──
                already  = c.execute(
                    "SELECT COALESCE(SUM(paid),0) as t FROM payments WHERE student_id=? AND month=?",
                    (sid, month)
                ).fetchone()["t"]
                stu_fees = float(stu_row["fees"] or 0)
                remaining = stu_fees - float(already)
                if remaining <= 0:
                    return err(f"هذا الطالب سدّد رسوم شهر {month} بالكامل — لا يمكن إضافة دفعة جديدة لنفس الشهر")
                if paid > remaining + 0.01:  # 0.01 tolerance for floats
                    return err(f"المبلغ المدفوع ({paid}) أكبر من المتبقي لهذا الشهر ({remaining:.2f})")
                # ── حفظ الدفعة ──
                new_total = float(already) + paid
                status = "paid" if new_total >= stu_fees else ("partial" if paid > 0 else "pending")
                c.execute("""INSERT INTO payments (student_id,paid,required,date,month,method,note,status,by_user)
                             VALUES (?,?,?,?,?,?,?,?,?)""",
                          (sid, paid, req,
                           body.get("date", datetime.now().strftime("%Y-%m-%d")),
                           month,
                           body.get("method","cash"), body.get("note",""), status, body.get("by","")))
                pid = c.lastrowid
                rno, rerr = _assign_payment_receipt_no(c, pid, body)
                if rerr:
                    return err(rerr)
                return ok({"id": pid, "status": status, "receipt_no": rno}, "تم تسجيل الدفعة")
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, body.get("caller_id")):
                    return err("🚫 حذف الدفعة يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM payments WHERE id=?", (body["id"],))
                return ok(msg="تم حذف الدفعة")
            if action == "fix_required":
                # إصلاح حقل required لكل الدفعات: required = المتبقي وقت تسجيل الدفعة
                pays = c.execute("SELECT p.*, s.fees FROM payments p JOIN students s ON p.student_id=s.id ORDER BY p.id").fetchall()
                # لكل طالب/شهر: احسب المتبقي قبل كل دفعة
                from collections import defaultdict
                paid_so_far = defaultdict(float)
                fixed = 0
                for p in pays:
                    key = (p["student_id"], p["month"])
                    fees = float(p["fees"] or 0)
                    remaining_before = max(0, fees - paid_so_far[key])
                    correct_req = min(float(p["paid"] or 0), remaining_before) if remaining_before > 0 else float(p["paid"] or 0)
                    correct_req = max(correct_req, float(p["paid"] or 0))  # required >= paid
                    correct_req = min(correct_req, remaining_before) if remaining_before > 0 else correct_req
                    # الصواب: required = المتبقي قبل هذه الدفعة
                    if abs(float(p["required"] or 0) - remaining_before) > 0.01:
                        c.execute("UPDATE payments SET required=? WHERE id=?", (remaining_before, p["id"]))
                        fixed += 1
                    paid_so_far[key] += float(p["paid"] or 0)
                return ok({"fixed": fixed}, f"تم إصلاح {fixed} دفعة")

    # ── REFUNDS (استرجاع للطالب) ──
    if resource == "refunds":
        if method == "GET":
            rows = c.execute(
                """SELECT r.*, s.name as student_name, s.code as student_code,
                c.name as course_name, c.group_name
                FROM refunds r
                LEFT JOIN students s ON r.student_id=s.id
                LEFT JOIN courses c ON s.course_id=c.id
                ORDER BY r.id DESC"""
            ).fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "save":
                return _refund_add_execute(c, body)
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, body.get("caller_id")):
                    return err("🚫 حذف الاسترجاع يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                ex = c.execute(
                    "SELECT payment_id FROM refunds WHERE id=?", (body["id"],)
                ).fetchone()
                if ex and ex["payment_id"]:
                    return err(
                        "استرجاع مرتبط بدفعة — استخدم «رد للدفع» لاستعادة المبلغ وإزالة السجل. "
                        "الحذف المباشر متاح لسجلات غير مرتبطة بدفعة فقط."
                    )
                c.execute("DELETE FROM refunds WHERE id=?", (body["id"],))
                return ok(msg="تم حذف سجل الاسترجاع")
            if action == "revert":
                return _refund_revert_execute(c, body)

    # ── GRADES ──
    if resource == "grades":
        if method == "GET":
            rows = c.execute("""SELECT g.*, s.name as student_name, s.code,
                c.name as course_name, c.group_name
                FROM grades g
                LEFT JOIN students s ON g.student_id=s.id
                LEFT JOIN courses c ON g.course_id=c.id
                ORDER BY g.id DESC""").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "add":
                c.execute("INSERT INTO grades (student_id,course_id,type,score,max_score,date,note) VALUES (?,?,?,?,?,?,?)",
                          (body["student_id"],body.get("course_id") or None,body.get("type","اختبار"),
                           body.get("score",0),body.get("max_score",100),body.get("date",""),body.get("note","")))
                return ok({"id":c.lastrowid},"تم إضافة التقييم")
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, body.get("caller_id")):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM grades WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── FOLLOWUPS ──
    if resource == "followups":
        if method == "GET":
            rows = c.execute("""SELECT f.*, s.name as student_name FROM followups f
                LEFT JOIN students s ON f.student_id=s.id ORDER BY f.id DESC""").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "add":
                c.execute("INSERT INTO followups (student_id,type,priority,note,date,by_user) VALUES (?,?,?,?,?,?)",
                          (body["student_id"],body.get("type",""),body.get("priority","medium"),
                           body.get("note",""),datetime.now().strftime("%Y-%m-%d"),body.get("by","")))
                return ok({"id":c.lastrowid},"تم إضافة الملاحظة")
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, body.get("caller_id")):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM followups WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── ATTENDANCE ──
    if resource == "attendance":
        if method == "GET":
            # للـ GET: date تأتي كـ action عند مسار مثل /api/attendance/{date}
            date = extra if extra else (action if action else datetime.now().strftime("%Y-%m-%d"))
            rows = c.execute("""SELECT a.*, s.name as student_name, s.code,
                c.name as course_name, c.group_name
                FROM attendance a
                LEFT JOIN students s ON a.student_id=s.id
                LEFT JOIN courses c ON s.course_id=c.id
                WHERE a.date=? ORDER BY s.name""", (date,)).fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "save":
                for rec in body.get("records",[]):
                    c.execute("""INSERT INTO attendance (student_id,date,present,note)
                                 VALUES (?,?,?,?)
                                 ON CONFLICT(student_id,date) DO UPDATE SET present=excluded.present, note=excluded.note""",
                              (rec["student_id"],rec["date"],1 if rec.get("present") else 0,rec.get("note","")))
                return ok(msg="تم حفظ الحضور")
            if action == "summary":
                rows = c.execute("""SELECT date, 
                    SUM(present) as present_count,
                    COUNT(*) - SUM(present) as absent_count,
                    COUNT(*) as total
                    FROM attendance GROUP BY date ORDER BY date DESC LIMIT 30""").fetchall()
                return ok(rows_to_list(rows))

    # ── EXPENSES ──
    if resource == "expenses":
        if method == "GET":
            rows = c.execute("SELECT * FROM expenses ORDER BY id DESC").fetchall()
            return ok(rows_to_list(rows))
        if method == "POST":
            if action == "add":
                c.execute("INSERT INTO expenses (name,category,amount,date,note) VALUES (?,?,?,?,?)",
                          (body["name"],body.get("category","أخرى"),body.get("amount",0),
                           body.get("date",datetime.now().strftime("%Y-%m-%d")),body.get("note","")))
                return ok({"id":c.lastrowid},"تم إضافة المصروف")
            if action == "delete":
                if not _caller_allowed_hard_delete(conn, body.get("caller_id")):
                    return err("🚫 الحذف يتطلب صلاحية «حذف السجلات» أو حساب مدير")
                c.execute("DELETE FROM expenses WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── USERS ──
    if resource == "users":
        caller_uid = _caller_id(body)
        caller_role = ""
        if caller_uid:
            cr = c.execute(
                "SELECT role, active FROM users WHERE id=?", (caller_uid,)
            ).fetchone()
            if cr and cr["active"]:
                caller_role = cr["role"] or ""

        if method == "GET":
            rows = c.execute("""SELECT id,name,username,role,active,perms,teacher_link_id,
                max_opens,opens_used,trial_message FROM users""").fetchall()
            result = []
            for r in rows:
                if r["role"] == "dev_master" and caller_role != "dev_master":
                    continue
                d = dict(r)
                d["perms"] = json.loads(d.get("perms") or "{}")
                result.append(d)
            return ok(result)
        if method == "POST":
            if action == "add":
                role_val = body.get("role", "receptionist")
                if role_val == "dev_master" and caller_role != "dev_master":
                    return err("غير مصرح — حساب المطور لا يُنشأ إلا من المطور")
                if c.execute("SELECT id FROM users WHERE username=?",(body["username"],)).fetchone():
                    return err("اسم المستخدم موجود بالفعل")
                perms = json.dumps(body.get("perms",{}))
                if role_val == "dev_master":
                    perms = json.dumps(DEV_MASTER_PERMS_FULL)
                # المدرس يجب أن يكون مربوط بمدرس
                if role_val == "teacher" and not body.get("teacher_link_id"):
                    return err("⚠️ المعلم غير مربوط بمدرس — يرجى ربطه من اختيار (ربط بمدرس)")
                # الحساب التجريبي يأخذ كل الصلاحيات ماعدا إدارة المستخدمين تلقائياً
                if role_val == "trial":
                    perms = json.dumps({"dash":1,"students":1,"attendance":1,"sessions":1,"grades":1,"followup":1,
                        "payments":1,"expenses":1,"reports":1,"courses":1,"teachers":1,"users":0,"settings":0,"about":1,"edit_fees":1,
                        "teacher_share":1,"manual_delete":0,"import_export":0,"online_users":0,"shared_database":0,"serial_licenses":0})
                c.execute("""INSERT INTO users
                    (name,username,password,role,active,perms,teacher_link_id,max_opens,opens_used,trial_message)
                    VALUES (?,?,?,?,1,?,?,?,0,?)""",
                          (body["name"],body["username"],hash_pw(body["password"]),
                           role_val,perms,body.get("teacher_link_id") or None,
                           body.get("max_opens") or None, body.get("trial_message") or None))
                return ok({"id":c.lastrowid},"تم إضافة المستخدم")
            if action == "update":
                tgt = c.execute("SELECT role FROM users WHERE id=?", (body["id"],)).fetchone()
                if not tgt:
                    return err("المستخدم غير موجود")
                if tgt["role"] == "dev_master" and caller_role != "dev_master":
                    return err("غير مصرح — لا يمكن تعديل حساب المطور")
                perms = json.dumps(body.get("perms",{}))
                role_val2 = body.get("role","receptionist")
                if role_val2 == "dev_master" and caller_role != "dev_master":
                    return err("غير مصرح")
                if role_val2 == "dev_master":
                    perms = json.dumps(DEV_MASTER_PERMS_FULL)
                if role_val2 == "teacher" and not body.get("teacher_link_id"):
                    return err("⚠️ المعلم غير مربوط بمدرس — يرجى ربطه من اختيار (ربط بمدرس)")
                if role_val2 == "trial":
                    perms = json.dumps({"dash":1,"students":1,"attendance":1,"sessions":1,"grades":1,"followup":1,
                        "payments":1,"expenses":1,"reports":1,"courses":1,"teachers":1,"users":0,"settings":0,"about":1,"edit_fees":1,
                        "teacher_share":1,"manual_delete":0,"import_export":0,"online_users":0,"shared_database":0,"serial_licenses":0})
                c.execute("""UPDATE users SET name=?,username=?,role=?,perms=?,teacher_link_id=?,
                    max_opens=?,trial_message=? WHERE id=?""",
                          (body["name"],body["username"],role_val2,
                           perms, body.get("teacher_link_id") or None,
                           body.get("max_opens") or None, body.get("trial_message") or None,
                           body["id"]))
                if body.get("password"):
                    c.execute("UPDATE users SET password=? WHERE id=?",(hash_pw(body["password"]),body["id"]))
                return ok(msg="تم التعديل")
            if action == "delete":
                if caller_role not in ("admin", "dev_master"):
                    return err("حذف المستخدمين متاح لمدير النظام أو مطور النظام فقط")
                if body["id"] == 1:
                    return err("لا يمكن حذف المدير الرئيسي")
                tgt = c.execute("SELECT role FROM users WHERE id=?", (body["id"],)).fetchone()
                if tgt and tgt["role"] == "dev_master" and caller_role != "dev_master":
                    return err("غير مصرح — لا يمكن حذف حساب المطور")
                c.execute("DELETE FROM users WHERE id=?", (body["id"],))
                return ok(msg="تم الحذف")

    # ── DASHBOARD ──
    if resource == "dashboard":
        total_students = c.execute("SELECT COUNT(*) FROM students WHERE status='active'").fetchone()[0]
        total_revenue  = c.execute("SELECT COALESCE(SUM(paid),0) FROM payments").fetchone()[0]
        total_expenses = c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses").fetchone()[0]
        # Late students
        late = c.execute("""SELECT s.id, s.name, s.code, s.fees, s.course_id,
            c.name as course_name, c.group_name,
            COALESCE(SUM(p.paid),0) as total_paid
            FROM students s
            LEFT JOIN payments p ON p.student_id=s.id
            LEFT JOIN courses c ON s.course_id=c.id
            WHERE s.status='active'
            GROUP BY s.id
            HAVING total_paid < s.fees""").fetchall()
        recent_students = c.execute("""SELECT s.*, c.name as course_name, c.group_name 
            FROM students s LEFT JOIN courses c ON s.course_id=c.id ORDER BY s.id DESC LIMIT 5""").fetchall()
        recent_payments = c.execute("""SELECT p.*, s.name as student_name, s.code FROM payments p
            LEFT JOIN students s ON p.student_id=s.id ORDER BY p.id DESC LIMIT 5""").fetchall()
        return ok({
            "total_students": total_students,
            "total_revenue": total_revenue,
            "total_expenses": total_expenses,
            "net_profit": total_revenue - total_expenses,
            "late_students": rows_to_list(late),
            "recent_students": rows_to_list(recent_students),
            "recent_payments": rows_to_list(recent_payments),
        })

    # ── REPORTS ──
    if resource == "reports":
        month = extra
        mf = f"{month}%" if month else "%"
        rev = c.execute("""SELECT COALESCE(SUM(p.paid),0) as total,
            c.name || ' - ' || c.group_name as course
            FROM payments p LEFT JOIN students s ON p.student_id=s.id
            LEFT JOIN courses c ON s.course_id=c.id
            WHERE p.date LIKE ? GROUP BY s.course_id""",(mf,)).fetchall()
        exp = c.execute("SELECT COALESCE(SUM(amount),0) as total, category FROM expenses WHERE date LIKE ? GROUP BY category",(mf,)).fetchall()
        att = c.execute("""SELECT date, SUM(present) as present, COUNT(*)-SUM(present) as absent, COUNT(*) as total
            FROM attendance WHERE date LIKE ? GROUP BY date ORDER BY date DESC LIMIT 10""",(mf,)).fetchall()
        late = c.execute("""SELECT s.*, c.name as course_name, c.group_name,
            COALESCE(SUM(p.paid),0) as paid_total
            FROM students s LEFT JOIN payments p ON p.student_id=s.id
            LEFT JOIN courses c ON s.course_id=c.id
            WHERE s.status='active' GROUP BY s.id HAVING paid_total < s.fees""").fetchall()
        total_rev = sum(r["total"] for r in rev)
        total_exp = sum(r["total"] for r in exp)
        ref_rows = c.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM refunds WHERE date LIKE ?", (mf,)
        ).fetchone()
        total_refunds = float(ref_rows["t"] or 0)
        orphan_ref = c.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM refunds WHERE date LIKE ? AND payment_id IS NULL",
            (mf,),
        ).fetchone()
        orphan_refunds = float(orphan_ref["t"] or 0)
        pay_rows = c.execute(
            """SELECT p.paid,
               s.center_share_type AS st_ct, s.center_share_value AS st_cv,
               tc.center_share_type AS t_ct, tc.center_share_value AS t_cv
               FROM payments p
               LEFT JOIN students s ON p.student_id=s.id
               LEFT JOIN courses c ON s.course_id=c.id
               LEFT JOIN teachers tc ON tc.id = COALESCE(s.reg_teacher_id, c.teacher_id)
               WHERE p.date LIKE ?""",
            (mf,),
        ).fetchall()
        center_total = 0.0
        teacher_total = 0.0
        for pr in pay_rows:
            paid = float(pr["paid"] or 0)
            st_ct = pr["st_ct"]
            use_stu = st_ct is not None and str(st_ct).strip() != ""
            if use_stu:
                eff_t = str(st_ct).strip().lower() or "percent"
                eff_v = float(pr["st_cv"] or 0)
            else:
                eff_t = pr["t_ct"] or "percent"
                eff_v = float(pr["t_cv"] or 0)
            ct, cv = _center_cut_from_payment(paid, eff_t, eff_v)
            center_total += ct
            teacher_total += cv
        # إيرادات الشهر = مجموع المدفوعات بعد خصم الاسترجاع من الدفعات المرتبطة؛
        # الاسترجاعات «اليتيمة» (بدون payment_id) تُخصم هنا فقط لتفادي الخصم مرتين.
        net_after_ref = total_rev - total_exp - orphan_refunds
        return ok(
            {
                "revenue_by_course": rows_to_list(rev),
                "expenses_by_cat": rows_to_list(exp),
                "attendance": rows_to_list(att),
                "late_students": rows_to_list(late),
                "total_revenue": total_rev,
                "total_expenses": total_exp,
                "total_refunds": total_refunds,
                "orphan_refunds": round(orphan_refunds, 2),
                "center_share_total": round(center_total, 2),
                "teacher_share_total": round(teacher_total, 2),
                "net": total_rev - total_exp,
                "net_after_refunds": round(net_after_ref, 2),
            }
        )

    # ── BILLING (ملخص محاسبة بالحصة) ──
    if resource == "billing":
        if method == "GET" and action == "session_totals":
            cid = _caller_id(body)
            allowed, _ = check_perm(conn, cid, "students", is_write=False)
            if not allowed:
                return err("ليس لديك صلاحية عرض بيانات الطلاب")
            sid_s = str(extra or body.get("student_id") or "").strip()
            try:
                sid = int(sid_s)
            except (TypeError, ValueError):
                sid = 0
            if not sid:
                return err("معرّف الطالب مطلوب")
            stu = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
            if not stu:
                return err("الطالب غير موجود")
            total_due, paid_total, cnt = _per_session_totals(c, stu)
            rem = max(0.0, total_due - paid_total)
            return ok(
                {
                    "student_id": sid,
                    "billing_mode": (stu["billing_mode"] or "monthly") or "monthly",
                    "session_count": cnt,
                    "session_fee": float(stu["fees"] or 0),
                    "total_due": total_due,
                    "paid_total": paid_total,
                    "remaining": rem,
                }
            )

    # ── STUDENT BALANCE ──
    if resource == "balance":
        if action == "check_month":
            sid   = body.get("student_id")
            month = body.get("month","")
            if not sid: return err("مطلوب id الطالب")
            stu = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
            if not stu: return err("الطالب غير موجود")
            bm = (stu["billing_mode"] or "monthly") or "monthly"
            if bm == "per_session":
                total_due, paid_total, scnt = _per_session_totals(c, stu)
                rem = max(0.0, total_due - paid_total)
                return ok({
                    "student_id": sid,
                    "month": month,
                    "billing_mode": "per_session",
                    "session_count": scnt,
                    "session_fee": float(stu["fees"] or 0),
                    "monthly_fees": total_due,
                    "paid_this_month": paid_total,
                    "remaining_this_month": rem,
                    "already_paid": rem <= 0.01,
                    "partial_paid": paid_total > 0 and rem > 0.01,
                })
            monthly_fees = float(stu["fees"] or 0)
            paid_this_month = float(c.execute(
                "SELECT COALESCE(SUM(paid),0) as t FROM payments WHERE student_id=? AND month=?",
                (sid, month)
            ).fetchone()["t"])
            remaining_this_month = max(0.0, monthly_fees - paid_this_month)
            return ok({
                "student_id": sid,
                "month": month,
                "monthly_fees": monthly_fees,
                "paid_this_month": paid_this_month,
                "remaining_this_month": remaining_this_month,
                "already_paid": paid_this_month >= monthly_fees,
                "partial_paid": 0 < paid_this_month < monthly_fees,
            })
        if action == "for_payment":
            pay_id = body.get("pay_id")
            pay = c.execute("SELECT * FROM payments WHERE id=?", (pay_id,)).fetchone()
            if not pay: return err("الدفعة غير موجودة")
            rem_this = float(pay["required"] or 0) - float(pay["paid"] or 0)
            stu = c.execute("SELECT fees FROM students WHERE id=?", (pay["student_id"],)).fetchone()
            return ok({
                "pay_id": pay_id,
                "this_required": float(pay["required"] or 0),
                "this_paid":     float(pay["paid"] or 0),
                "this_remaining": rem_this,
                "monthly_fees":  float(stu["fees"]) if stu else 0,
            })
        # /balance/{sid} → action=sid, extra=""
        sid   = int(action) if action and action.isdigit() else (int(extra) if extra else 0)
        month = body.get("month", "")
        stu   = c.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        if not stu: return err("الطالب غير موجود")
        monthly_fees = float(stu["fees"] or 0)

        bm = (stu["billing_mode"] or "monthly") or "monthly"
        if month:
            if bm == "per_session":
                total_due, paid_total, scnt = _per_session_totals(c, stu)
                rem = max(0.0, total_due - paid_total)
                return ok({
                    "student_id":   sid,
                    "month":        month,
                    "billing_mode": "per_session",
                    "session_count": scnt,
                    "monthly_fees": total_due,
                    "paid_month":   float(paid_total),
                    "remaining":    rem,
                    "overpaid":     False,
                })
            # كم دفع الطالب في هذا الشهر تحديداً
            paid_this_month = c.execute(
                "SELECT COALESCE(SUM(paid),0) as t FROM payments WHERE student_id=? AND month=?",
                (sid, month)
            ).fetchone()["t"]
            remaining_this_month = monthly_fees - float(paid_this_month)
            return ok({
                "student_id":   sid,
                "month":        month,
                "monthly_fees": monthly_fees,
                "paid_month":   float(paid_this_month),
                "remaining":    max(0.0, remaining_this_month),
                "overpaid":     remaining_this_month < 0,
            })
        else:
            # بدون شهر: إجمالي تراكمي
            if bm == "per_session":
                total_due, paid_total, scnt = _per_session_totals(c, stu)
                rem = max(0.0, total_due - paid_total)
                return ok({
                    "student_id":   sid,
                    "billing_mode": "per_session",
                    "session_count": scnt,
                    "monthly_fees": total_due,
                    "paid_total":   float(paid_total),
                    "remaining":    rem,
                })
            paid_total = c.execute(
                "SELECT COALESCE(SUM(paid),0) as t FROM payments WHERE student_id=?", (sid,)
            ).fetchone()["t"]
            return ok({
                "student_id":   sid,
                "monthly_fees": monthly_fees,
                "paid_total":   float(paid_total),
                "remaining":    max(0.0, monthly_fees - float(paid_total)),
            })

    return err(f"مسار غير موجود: {resource}/{action}")


# ─── HTTP SERVER ─────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # silent

    def _safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # العميل قفل الاتصال أثناء إرسال الرد — تجاهل
            return

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._safe_write(body)

    def send_html(self, path):
        try:
            with open(path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self._safe_write(content)
        except FileNotFoundError:
            self.send_error(404, "File not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/app":
            self.send_html(HTML_PATH)
        elif path.startswith("/api/"):
            # parse query string into body for GET requests
            qs = parsed.query
            body = {}
            if qs:
                for kv in qs.split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        body[unquote_plus(k)] = unquote_plus(v)
            api_path = path[len("/api") :] if path.startswith("/api") else path
            result = handle_api("GET", api_path, body, self.headers)
            self.send_json(result)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self.send_error(404); return
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except:
                pass
        api_path = path[len("/api") :] if path.startswith("/api") else path
        result = handle_api("POST", api_path, body, self.headers)
        self.send_json(result)


# ─── MAIN ────────────────────────────────────────────
def main():
    print("=" * 50)
    print("سنتر الدروس الخصوصية - نظام الإدارة")
    print("=" * 50)
    init_db()
    # initialize revision + watcher (detect external DB changes too)
    _bump_db_rev("startup")
    # ملاحظة: مراقبة ملفات SQLite (خصوصاً WAL) على ويندوز قد تُسبب إشعارات تغيّر متكررة
    # حتى بدون تغيّر فعلي في البيانات، مما يؤدي لتحديثات أمامية متتابعة (تبربش).
    # نعتمد هنا على bump بعد عمليات الكتابة عبر السيرفر (وهي المسار الطبيعي لكل المستخدمين).
    # لو احتجت لاحقاً مراقبة تغييرات خارجية للملف، يمكن إعادة تفعيلها بشرط ضبط منطق أدق.
    # start_db_file_watcher()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    import socket as _sock
    try:
        local_ip = _sock.gethostbyname(_sock.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    url = f"http://127.0.0.1:{PORT}"
    url_net = f"http://{local_ip}:{PORT}"
    print(f"الرابط المحلي:  {url}")
    print(f"الرابط الشبكي:  {url_net}")
    print(f"قاعدة البيانات: {DB_PATH}")
    print(f"تسجيل الدخول: admin / 1234")
    print("لإيقاف السيرفر اضغط Ctrl+C")
    print("-" * 50)

    # No browser on Railway/cloud
    if not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("CENTER_NO_BROWSER","")=="1"):
        def open_browser():
            import time
            time.sleep(1)
            try: webbrowser.open(url)
            except: pass
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nتم إيقاف السيرفر")
        server.shutdown()

if __name__ == "__main__":
    main()
