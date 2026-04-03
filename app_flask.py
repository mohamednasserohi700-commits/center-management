#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from flask import Flask, request, jsonify, send_file

# نستخدم منطق الـ API الموجود في server.py كما هو
import server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


@app.after_request
def add_headers(resp):
    # نفس الهيدرز الأساسية في النسخة المحلية
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/", methods=["GET"])
@app.route("/app", methods=["GET"])
def index():
    return send_file(server.HTML_PATH)


@app.route("/api/<path:api_path>", methods=["GET", "POST", "OPTIONS"])
def api(api_path: str):
    if request.method == "OPTIONS":
        return ("", 200)

    # توافق مع handle_api: path يكون مثل /meta/version
    path = "/" + (api_path or "").lstrip("/")

    if request.method == "GET":
        body = {k: v for k, v in request.args.items()}
        result = server.handle_api("GET", path, body, request.headers)
        return jsonify(result)

    # POST
    body = request.get_json(silent=True) or {}
    result = server.handle_api("POST", path, body, request.headers)
    return jsonify(result)


@app.route("/api", methods=["GET"])
def api_root():
    # مساعدة بسيطة لو حد نادى /api بالغلط
    return jsonify({"ok": False, "msg": "use /api/<resource>[/action]"})


if __name__ == "__main__":
    # تشغيل محلي (للاختبار) — في الاستضافة هنستخدم WSGI/Gunicorn
    app.run(host="127.0.0.1", port=7788, debug=False)

