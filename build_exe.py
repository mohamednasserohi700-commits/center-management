#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بناء ملف تنفيذي (EXE) لسنتر الدروس الخصوصية باستخدام PyInstaller.

الاستخدام:
  python build_exe.py                  → ملف exe واحد (onefile) + أيقونة إن وُجدت
  python build_exe.py --folder       → مجلد (أسرع تشغيلاً للتجربة)
  python build_exe.py --icon مسار.ico

ضع أيقونة باسم app.ico أو center.ico أو icon.ico بجانب هذا السكربت، أو مرّر --icon
"""

import argparse
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXE_NAME = "CenterLessons"


def _need(module: str) -> None:
    try:
        __import__(module)
    except ImportError:
        print(f"📦 تثبيت {module}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", module], cwd=BASE)


def _find_icon(cli_icon: str):
    if cli_icon and os.path.isfile(cli_icon):
        return os.path.abspath(cli_icon)
    for name in ("app.ico", "center.ico", "icon.ico"):
        p = os.path.join(BASE, name)
        if os.path.isfile(p):
            return p
    return None


def build() -> int:
    ap = argparse.ArgumentParser(description="بناء EXE لسنتر الدروس الخصوصية")
    ap.add_argument(
        "--folder",
        action="store_true",
        help="إخراج مجلد (onedir) بدل ملف واحد — أسرع عند التشغيل المتكرر",
    )
    ap.add_argument("--icon", default="", help="مسار ملف .ico")
    ap.add_argument(
        "--name",
        default=DEFAULT_EXE_NAME,
        help="اسم الـ exe (يفضّل أحرف لاتينية بدون مسافات)",
    )
    ap.add_argument(
        "--console",
        action="store_true",
        help="إظهار نافذة الطرفية (للتشخيص عند فشل التشغيل)",
    )
    args = ap.parse_args()

    print("=" * 55)
    print("🏗️  بناء ملف EXE — سنتر الدروس الخصوصية")
    print("=" * 55)

    _need("PyInstaller")
    _need("pywebview")

    app_html = os.path.join(BASE, "app.html")
    server_py = os.path.join(BASE, "server.py")
    launcher_py = os.path.join(BASE, "launcher.py")
    for p, label in ((app_html, "app.html"), (server_py, "server.py"), (launcher_py, "launcher.py")):
        if not os.path.isfile(p):
            print(f"❌ الملف مفقود: {label}")
            return 1

    sep = os.pathsep
    data_app = f"{app_html}{sep}."
    data_srv = f"{server_py}{sep}."

    icon_path = _find_icon((args.icon or "").strip())
    if icon_path:
        print(f"✅ أيقونة: {icon_path}")
    else:
        print("ℹ️  بدون أيقونة مخصّصة — أضف app.ico بجانب build_exe.py أو استخدم --icon")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        args.name,
        "--distpath",
        os.path.join(BASE, "dist"),
        "--workpath",
        os.path.join(BASE, "build"),
        "--specpath",
        BASE,
        "--add-data",
        data_app,
        "--add-data",
        data_srv,
        # pywebview على ويندوز (WebView2 / winforms)
        "--hidden-import",
        "webview",
        "--hidden-import",
        "webview.platforms.winforms",
        "--hidden-import",
        "clr",
        "--collect-all",
        "webview",
        # تشفير السريال إن وُجد
        "--hidden-import",
        "cryptography.fernet",
        "--collect-all",
        "cryptography",
        "--hidden-import",
        "sqlite3",
        "--hidden-import",
        "importlib.util",
        launcher_py,
    ]

    if args.console:
        cmd.insert(3, "--console")
    else:
        cmd.insert(3, "--windowed")

    if args.folder:
        cmd.insert(4, "--onedir")
        print("📁 وضع الإخراج: مجلد (onedir)")
    else:
        cmd.insert(4, "--onefile")
        print("📦 وضع الإخراج: ملف exe واحد (onefile)")

    if icon_path:
        cmd.extend(["--icon", icon_path])

    print("\n🔨 جاري البناء (قد يستغرق عدة دقائق)...\n")
    r = subprocess.run(cmd, cwd=BASE)
    if r.returncode != 0:
        print("\n❌ فشل البناء — راجع الرسائل أعلاه.")
        return r.returncode

    if args.folder:
        out_dir = os.path.join(BASE, "dist", args.name)
        exe_guess = os.path.join(out_dir, f"{args.name}.exe")
    else:
        out_dir = os.path.join(BASE, "dist")
        exe_guess = os.path.join(out_dir, f"{args.name}.exe")

    print("\n✅ تم البناء بنجاح")
    print(f"📁 المجلد: {out_dir}")
    if os.path.isfile(exe_guess):
        print(f"▶️  التشغيل: {exe_guess}")
    print(
        "\nملاحظات:\n"
        " • قاعدة البيانات center.db تُنشأ بجانب الـ exe (للنسخة onefile).\n"
        " • ثبّت WebView2 Runtime على ويندوز إن لم تكن مثبّتة (مطلوب لنافذة pywebview).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(build())
