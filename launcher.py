#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
سنتر الدروس الخصوصية — تشغيل كنافذة مستقلة
يعمل بدون مستعرض خارجي
"""

import os, sys, threading, time, subprocess, socket, re, atexit, traceback

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 7788
URL  = f"http://127.0.0.1:{PORT}"

# ─── KIOSK / TASKBAR (Windows) ───────────────────────
_TASKBAR_HIDDEN = False


def _launcher_log_path() -> str:
    return os.path.join(BASE_DIR, "center_launcher.log")


def log(msg: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_launcher_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def show_error(title: str, message: str) -> None:
    """Show a visible error to the user (works with pythonw)."""
    try:
        log(f"{title}: {message}")
        if os.name == "nt":
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), MB_OK | MB_ICONERROR)
            return
    except Exception:
        pass
    # fallback (console)
    try:
        print(title)
        print(message)
    except Exception:
        pass


def _hide_taskbar_windows() -> bool:
    """Hide taskbar + start button. Best-effort; returns True if attempted."""
    global _TASKBAR_HIDDEN
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL

        SW_HIDE = 0
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            user32.ShowWindow(tray, SW_HIDE)
        start = user32.FindWindowW("Button", None)  # legacy
        if start:
            user32.ShowWindow(start, SW_HIDE)
        _TASKBAR_HIDDEN = True
        return True
    except Exception:
        return False


def _show_taskbar_windows() -> bool:
    global _TASKBAR_HIDDEN
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL

        SW_SHOW = 5
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            user32.ShowWindow(tray, SW_SHOW)
        start = user32.FindWindowW("Button", None)
        if start:
            user32.ShowWindow(start, SW_SHOW)
        _TASKBAR_HIDDEN = False
        return True
    except Exception:
        return False


def _ensure_taskbar_restored():
    if _TASKBAR_HIDDEN:
        _show_taskbar_windows()


atexit.register(_ensure_taskbar_restored)


def _find_edge_exe() -> str:
    if os.name != "nt":
        return ""
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return "msedge"


def _find_chrome_exe() -> str:
    if os.name != "nt":
        return ""
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return "chrome"


def open_kiosk_browser(url: str) -> int:
    """Open Edge/Chrome in kiosk full screen; returns PID or 0."""
    if os.name != "nt":
        return 0
    # Prefer Edge kiosk (works on Windows 10/11)
    edge = _find_edge_exe()
    try:
        p = subprocess.Popen(
            [
                edge,
                "--kiosk",
                url,
                "--edge-kiosk-type=fullscreen",
                "--no-first-run",
                "--new-window",
            ],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(p.pid or 0)
    except Exception:
        pass

    # Fallback Chrome kiosk
    chrome = _find_chrome_exe()
    try:
        p = subprocess.Popen(
            [chrome, "--kiosk", "--no-first-run", "--new-window", url],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(p.pid or 0)
    except Exception:
        return 0


def wait_pid_exit(pid: int) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            # tasklist is cheap and available
            while True:
                out = subprocess.check_output(
                    ["cmd", "/c", "tasklist", "/fi", f"PID eq {pid}"],
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                )
                if str(pid) not in out:
                    return
                time.sleep(0.5)
        else:
            os.waitpid(pid, 0)
    except Exception:
        return


# ─── 1. تشغيل السيرفر في الخلفية ────────────────────
def kill_port_process(port: int) -> bool:
    """اقتل أي عملية ماسكة المنفذ (Windows) لضمان تحديث السيرفر."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, encoding="utf-8", errors="ignore")
    except Exception:
        return False
    pids = set()
    for line in out.splitlines():
        # مثال: TCP    127.0.0.1:7788   0.0.0.0:0   LISTENING   12345
        if f":{port}" not in line:
            continue
        m = re.search(r"\sLISTENING\s+(\d+)\s*$", line)
        if m:
            pids.add(m.group(1))
    killed = False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed = True
        except Exception:
            pass
    return killed

def is_port_open(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.6)
        s.close()
        return True
    except OSError:
        return False

def start_server():
    server_path = os.path.join(BASE_DIR, "server.py")
    # لو فيه نسخة قديمة ماسكة البورت، اقفلها قبل التشغيل (وإلا التحديث مش هيظهر)
    kill_port_process(PORT)
    # لو ما زال المنفذ شغال بعد القتل → اطبع تحذير واضح بدل ما نكمل بصمت
    time.sleep(0.4)
    if is_port_open(PORT):
        print("❌ لا يزال المنفذ مستخدماً بعد محاولة الإغلاق.")
        print("✅ افتح Task Manager واقفل أي python.exe ماسك PORT 7788 ثم شغل البرنامج مرة أخرى.")
        return
    log_path = os.path.join(BASE_DIR, "center_server.log")
    try:
        logf = open(log_path, "a", encoding="utf-8")
    except Exception:
        logf = None
    env = os.environ.copy()
    # منع server.py من فتح المتصفح تلقائياً (الواجهة ستكون داخل pywebview)
    env["CENTER_NO_BROWSER"] = "1"
    log(f"Starting server: {server_path}")
    subprocess.Popen(
        [sys.executable, server_path],
        cwd=BASE_DIR,
        env=env,
        stdout=logf or subprocess.DEVNULL,
        stderr=logf or subprocess.DEVNULL,
    )

def wait_for_server(timeout=10):
    """انتظر حتى يبدأ السيرفر"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False

# ─── 2. فتح نافذة البرنامج (بدون متصفح) ───────────────
def try_webview():
    try:
        import webview
        log("pywebview imported successfully")
        _hide_taskbar_windows()
        window = webview.create_window(
            title   = "سنتر الدروس الخصوصية",
            url     = URL,
            width   = 1280,
            height  = 800,
            min_size= (800, 600),
            resizable = True,
            fullscreen = True,
        )
        try:
            log("Starting webview window")
            webview.start()
        finally:
            _show_taskbar_windows()
            try:
                log("Closing: stopping server on port %s" % PORT)
                kill_port_process(PORT)
            except Exception:
                pass
        return True
    except ImportError:
        log("ImportError: pywebview not installed")
        return False
    except Exception as e:
        log("webview error: " + repr(e))
        log(traceback.format_exc())
        return False

# ─── 3. (تم تعطيل الفولباك) ───────────────────────────
def try_tkinter_browser():
    try:
        import tkinter as tk
        from tkinter import ttk
        import webbrowser

        root = tk.Tk()
        root.title("سنتر الدروس الخصوصية")
        # وضع ملء الشاشة الكامل (يغطي شريط المهام على ويندوز)
        try:
            root.attributes("-fullscreen", True)
        except Exception:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            root.geometry(f"{sw}x{sh}+0+0")
            root.state("zoomed")
        root.resizable(True, True)

        # Icon workaround
        try:
            root.iconbitmap(default='')
        except:
            pass

        # Style
        root.configure(bg="#0f2744")

        frame = tk.Frame(root, bg="#0f2744")
        frame.pack(expand=True, fill="both", padx=30, pady=30)

        # Emoji + Title
        tk.Label(frame, text="سنتر", font=("Arial", 32, "bold"), bg="#0f2744", fg="white").pack(pady=(16,6))
        tk.Label(frame, text="سنتر الدروس الخصوصية",
                 font=("Arial", 18, "bold"), bg="#0f2744", fg="white").pack()
        tk.Label(frame, text="نظام الإدارة المتكامل",
                 font=("Arial", 12), bg="#0f2744", fg="#94a3b8").pack(pady=(4,20))

        status_var = tk.StringVar(value="جاري تشغيل السيرفر...")
        status_lbl = tk.Label(frame, textvariable=status_var,
                               font=("Arial", 11), bg="#0f2744", fg="#f59e0b")
        status_lbl.pack(pady=5)

        def open_browser():
            time.sleep(0.5)
            webbrowser.open(URL)
            status_var.set(f"البرنامج يعمل على: {URL}")

        def on_open():
            threading.Thread(target=open_browser, daemon=True).start()

        btn_frame = tk.Frame(frame, bg="#0f2744")
        btn_frame.pack(pady=15)

        open_btn = tk.Button(btn_frame, text="فتح البرنامج",
                              font=("Arial", 13, "bold"), bg="#f59e0b", fg="#0f2744",
                              relief="flat", padx=20, pady=10, cursor="hand2",
                              command=on_open)
        open_btn.pack(side="left", padx=5)

        quit_btn = tk.Button(btn_frame, text="إغلاق",
                              font=("Arial", 12), bg="#ef4444", fg="white",
                              relief="flat", padx=15, pady=10, cursor="hand2",
                              command=root.destroy)
        quit_btn.pack(side="left", padx=5)

        tk.Label(frame, text=f"يعمل على: {URL}",
                 font=("Arial", 10), bg="#0f2744", fg="#475569").pack(pady=5)

        # Auto-open after 1 second
        root.after(1200, on_open)

        _hide_taskbar_windows()
        def _on_close():
            try:
                _show_taskbar_windows()
            finally:
                root.destroy()
        root.protocol("WM_DELETE_WINDOW", _on_close)

        root.mainloop()
        _show_taskbar_windows()
        return True
    except Exception as e:
        print(f"tkinter error: {e}")
        _show_taskbar_windows()
        return False

# ─── 4. (تم تعطيل الفولباك) ───────────────────────────
def fallback_browser():
    # استخدم Kiosk mode لو متاح (ملء شاشة حقيقي + بدون شريط)
    _hide_taskbar_windows()
    pid = open_kiosk_browser(URL)
    if pid:
        try:
            wait_pid_exit(pid)
        finally:
            _show_taskbar_windows()
        return
    # آخر حل: افتح المتصفح العادي
    import webbrowser
    webbrowser.open(URL)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _show_taskbar_windows()

# ─── MAIN ─────────────────────────────────────────────
def main():
    log("=" * 30 + " launcher start " + "=" * 30)
    log(f"Python: {sys.executable}")
    log(f"Base dir: {BASE_DIR}")

    # Start server
    start_server()

    if not wait_for_server(timeout=12):
        show_error(
            "فشل تشغيل السيرفر",
            "لم يبدأ السيرفر خلال الوقت المحدد.\n"
            "افتح ملف center_server.log لمعرفة السبب.\n"
            "وتأكد أن المنفذ 7788 غير مستخدم.",
        )
        sys.exit(1)

    log(f"Server is up: {URL}")

    # فتح البرنامج بدون متصفح: pywebview (WebView2)
    if not try_webview():
        show_error(
            "تعذر فتح نافذة البرنامج",
            "لم نتمكن من فتح نافذة pywebview.\n\n"
            "أسباب شائعة:\n"
            "- WebView2 Runtime غير مثبت على ويندوز\n"
            "- مشكلة في pywebview / صلاحيات\n\n"
            "راجع ملف center_launcher.log لمعرفة الخطأ بالتفصيل.",
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
