#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local web UI for CSDN expert-annotation drafts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

from csdn_annotation_draft import AnnotationDraft, build_drafts, fetch_html, parse_article


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csdn_annotation_ui.log")
CODEX_TIMEOUT_SECONDS = 180
BROWSER_IMPORT: Dict[str, Any] = {"url": "", "title": "", "html": "", "receivedAt": ""}
BROWSER_IMPORT_LOCK = threading.Lock()


def log_event(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


def safe_console_print(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        log_event(message)


def client_error_message(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        server = exc.headers.get("Server", "")
        if exc.code == 521 and server.upper() == "WAF":
            return (
                "抓取网页失败：CSDN 返回了 WAF 反爬校验（HTTP 521），本地脚本不能直接读取这篇文章。"
                "请在浏览器正常打开文章，复制文章标题和正文，回到工具页面粘贴到“文章正文 / HTML”文本框，"
                "也可以点击“从剪贴板粘贴”后再生成。"
            )
        reason = str(exc.reason or "").strip()
        return f"抓取网页失败：目标网站返回 HTTP {exc.code}{('，' + reason) if reason else ''}。"
    if isinstance(exc, urllib.error.URLError):
        return f"抓取网页失败：无法连接目标网站，{exc.reason}。可以改用粘贴 HTML 方式生成。"
    return str(exc)


def error_status(exc: Exception) -> HTTPStatus:
    if isinstance(exc, (urllib.error.HTTPError, urllib.error.URLError)):
        return HTTPStatus.BAD_GATEWAY
    return HTTPStatus.BAD_REQUEST


def log_exception(exc: Exception) -> None:
    if isinstance(exc, urllib.error.HTTPError):
        server = exc.headers.get("Server", "")
        request_id = exc.headers.get("X-Request-Id", "")
        log_event(
            "request failed "
            f"type=HTTPError code={exc.code} reason={exc.reason!r} "
            f"server={server!r} requestId={request_id!r}"
        )
        return
    log_event(f"request failed type={type(exc).__name__} error={exc}")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CSDN 专家标注草稿生成器</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #64748b;
      --line: #d8dee9;
      --accent: #2563eb;
      --accent-dark: #1d4ed8;
      --error: #b91c1c;
      --ok: #047857;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
    }
    header {
      background: #172033;
      color: #fff;
      padding: 18px 28px;
      border-bottom: 1px solid #0f172a;
    }
    header h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 430px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
      max-width: 1440px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    label {
      display: block;
      margin: 12px 0 6px;
      color: #334155;
      font-weight: 600;
    }
    input[type="url"], textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 11px;
      color: var(--text);
      background: #fff;
      font: inherit;
      outline: none;
    }
    input[type="url"]:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, .12);
    }
    textarea {
      min-height: 180px;
      resize: vertical;
      line-height: 1.6;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      align-items: end;
    }
    .check {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-top: 12px;
      color: var(--muted);
      user-select: none;
    }
    .actions {
      display: flex;
      gap: 10px;
      margin-top: 16px;
      flex-wrap: wrap;
    }
    .import-actions {
      display: flex;
      gap: 10px;
      margin-top: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 9px 13px;
      font: inherit;
      cursor: pointer;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-dark); }
    button:disabled {
      opacity: .55;
      cursor: not-allowed;
    }
    .hint {
      margin-top: 10px;
      color: var(--muted);
      line-height: 1.6;
    }
    .hint.small {
      font-size: 13px;
    }
    .status {
      min-height: 22px;
      margin-top: 12px;
      color: var(--muted);
    }
    .status.error { color: var(--error); }
    .status.ok { color: var(--ok); }
    .quota-warning {
      margin-top: 10px;
      color: #b91c1c;
      font-weight: 700;
      line-height: 1.6;
    }
    .summary {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 12px;
      color: var(--muted);
    }
    .summary strong {
      display: block;
      color: var(--text);
      font-size: 17px;
      margin-bottom: 3px;
    }
    .cards {
      display: grid;
      gap: 12px;
    }
    .card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .type {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 9px;
      border-radius: 999px;
      background: #eef4ff;
      color: #1d4ed8;
      font-weight: 650;
    }
    .reason {
      color: var(--muted);
      margin-bottom: 10px;
    }
    .block-title {
      margin: 12px 0 6px;
      font-weight: 650;
      color: #334155;
    }
    .box {
      border: 1px solid #e2e8f0;
      background: #f8fafc;
      border-radius: 6px;
      padding: 10px;
      line-height: 1.7;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .empty {
      min-height: 260px;
      display: grid;
      place-items: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcff;
      text-align: center;
      line-height: 1.7;
      padding: 20px;
    }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>CSDN 专家标注草稿生成器</h1>
  </header>
  <main>
    <section>
      <form id="form">
        <label for="url">文章 URL</label>
        <input id="url" type="url" placeholder="https://blog.csdn.net/..." />

        <label for="html">或粘贴文章正文 / HTML</label>
        <textarea id="html" placeholder="更简单的方式：在浏览器打开文章，复制文章正文，粘贴到这里。也可以粘贴网页 HTML。"></textarea>
        <div class="import-actions">
          <button id="pasteClipboard" type="button">从剪贴板粘贴</button>
        </div>
        <div class="hint small">
          遇到 WAF 521 时，直接复制文章正文粘贴即可，不需要使用浏览器导入书签。
        </div>

        <div class="row">
          <div>
            <label for="max">生成数量</label>
            <select id="max">
              <option value="5">5 条</option>
              <option value="4">4 条</option>
              <option value="3">3 条</option>
              <option value="2">2 条</option>
              <option value="1">1 条</option>
            </select>
          </div>
          <label class="check">
            <input id="insecure" type="checkbox" checked />
            跳过 HTTPS 证书校验
          </label>
        </div>
        <label class="check">
          <input id="useCodex" type="checkbox" />
          使用本机 Codex 优化草稿
        </label>
        <label class="check">
          <input id="fullText" type="checkbox" />
          生成全文标注
        </label>

        <div class="actions">
          <button class="primary" id="generate" type="submit">生成标注草稿</button>
          <button id="clear" type="button">清空</button>
        </div>
        <div id="status" class="status"></div>
      </form>
      <div class="hint">
        该工具只生成草稿，不会自动提交。CSDN 专家标注仍需你人工选择原文、确认内容并提交审核。
        <div class="quota-warning">额度：每人每天最多 10 处、每篇文章最多 5 处；片段标注和全文标注共用同一额度，全文标注也按 1 处计算。</div>
      </div>
    </section>

    <section>
      <div class="summary">
        <div>
          <strong id="title">等待生成</strong>
          <span id="count">暂无标注草稿</span>
          <div class="quota-warning" style="margin-top:4px;">额度：每天最多 10 处，每篇最多 5 处；全文标注同样计入。</div>
        </div>
        <button id="copyAll" type="button" disabled>复制全部</button>
      </div>
      <div id="cards" class="cards">
        <div class="empty">输入 URL 后点击生成。<br />如果网络抓取失败，可粘贴 HTML 再生成。</div>
      </div>
    </section>
  </main>

  <script>
    const form = document.getElementById('form');
    const statusEl = document.getElementById('status');
    const cardsEl = document.getElementById('cards');
    const titleEl = document.getElementById('title');
    const countEl = document.getElementById('count');
    const copyAllBtn = document.getElementById('copyAll');
    const generateBtn = document.getElementById('generate');
    let lastPayload = null;
    let statusTimer = null;
    let lastImportAt = '';

    function setStatus(message, kind = '') {
      statusEl.textContent = message;
      statusEl.className = 'status ' + kind;
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    async function copyText(text) {
      await navigator.clipboard.writeText(text);
      setStatus('已复制到剪贴板', 'ok');
    }

    function buildBookmarklet() {
      const importUrl = `${location.origin}/import`;
      const importOrigin = location.origin;
      const script = `(() => {
        const data = {
          source: 'csdn-annotation-tool',
          url: location.href,
          title: document.title,
          html: document.documentElement.outerHTML
        };
        const win = window.open(${JSON.stringify(importUrl)}, 'csdn_annotation_import');
        if (!win) {
          alert('浏览器阻止了导入窗口，请允许弹出窗口后重试。');
          return;
        }
        let attempts = 0;
        const timer = setInterval(() => {
          attempts += 1;
          win.postMessage(data, ${JSON.stringify(importOrigin)});
          if (attempts >= 20) clearInterval(timer);
        }, 500);
      })();`;
      return 'javascript:' + encodeURIComponent(script);
    }

    async function loadBrowserImport(showEmptyMessage = true) {
      const params = new URLSearchParams({ ts: String(Date.now()) });
      if (lastImportAt) params.set('since', lastImportAt);
      const response = await fetch('/api/import/latest?' + params.toString());
      const payload = await response.json();
      if (payload.unchanged) return Boolean(document.getElementById('html').value.trim());
      if (!payload.html) {
        if (showEmptyMessage) setStatus('暂未收到浏览器导入 HTML。', 'error');
        return false;
      }
      if (payload.receivedAt === lastImportAt && !showEmptyMessage) return true;
      lastImportAt = payload.receivedAt || '';
      document.getElementById('url').value = payload.url || '';
      document.getElementById('html').value = payload.html || '';
      setStatus(`已导入浏览器页面 HTML：${payload.title || payload.url || '未识别标题'}`, 'ok');
      return true;
    }

    function formatAll(payload) {
      return payload.annotations.map((item, index) =>
        item.fullText
          ? `标注 ${index + 1}\n类型：${item.type}\n标注范围：全文\n标注内容：${item.content}`
          : `标注 ${index + 1}\n类型：${item.type}\n建议选中文本：${item.selectedText}\n标注内容：${item.content}`
      ).join('\n\n');
    }

    function render(payload) {
      lastPayload = payload;
      titleEl.textContent = payload.title || '未识别标题';
      countEl.textContent = `共 ${payload.annotations.length} 条标注草稿` + (payload.provider ? ` · ${payload.provider}` : '');
      if (payload.warning) {
        setStatus(payload.warning, 'error');
      }
      copyAllBtn.disabled = payload.annotations.length === 0;
      if (!payload.annotations.length) {
        cardsEl.innerHTML = '<div class="empty">没有生成可用草稿，请换一篇文章或粘贴完整 HTML。</div>';
        return;
      }
      cardsEl.innerHTML = payload.annotations.map((item, index) => `
        <article class="card">
          <div class="card-head">
            <span class="type">${escapeHtml(item.type)}</span>
            <button type="button" data-copy="${index}">复制本条</button>
          </div>
          <div class="reason">推荐原因：${escapeHtml(item.reason)}</div>
          <div class="block-title">${item.fullText ? '标注范围' : '建议选中文本'}</div>
          <div class="box">${item.fullText ? '全文标注，无需选中文本' : escapeHtml(item.selectedText)}</div>
          <div class="block-title">标注内容</div>
          <div class="box">${escapeHtml(item.content)}</div>
        </article>
      `).join('');
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const body = {
        url: document.getElementById('url').value.trim(),
        html: document.getElementById('html').value,
        max: Number(document.getElementById('max').value),
        insecure: document.getElementById('insecure').checked,
        useCodex: document.getElementById('useCodex').checked,
        fullText: document.getElementById('fullText').checked
      };
      if (!body.url && !body.html.trim()) {
        setStatus('请填写文章 URL 或粘贴 HTML。', 'error');
        return;
      }
      if (!body.html.trim()) {
        await loadBrowserImport(false).catch(() => false);
        body.url = document.getElementById('url').value.trim();
        body.html = document.getElementById('html').value;
      }
      generateBtn.disabled = true;
      const startedAt = Date.now();
      setStatus(body.useCodex ? '正在调用本机 Codex，通常需要 30-180 秒...' : '正在生成...');
      statusTimer = setInterval(() => {
        const seconds = Math.floor((Date.now() - startedAt) / 1000);
        if (body.useCodex) {
          setStatus(`正在调用本机 Codex，已等待 ${seconds} 秒。可查看启动窗口或 csdn_annotation_ui.log。`);
        } else {
          setStatus(`正在生成，已等待 ${seconds} 秒。`);
        }
      }, 1000);
      try {
        const response = await fetch('/api/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || '生成失败');
        }
        render(payload);
        setStatus('生成完成', 'ok');
      } catch (error) {
        setStatus(error.message, 'error');
      } finally {
        if (statusTimer) {
          clearInterval(statusTimer);
          statusTimer = null;
        }
        generateBtn.disabled = false;
      }
    });

    document.getElementById('pasteClipboard').addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (!text.trim()) {
          setStatus('剪贴板里没有可用内容。请先在文章页面复制正文。', 'error');
          return;
        }
        document.getElementById('html').value = text;
        setStatus('已从剪贴板粘贴内容，可以点击生成。', 'ok');
      } catch (error) {
        setStatus('浏览器不允许读取剪贴板，请手动按 Ctrl+V 粘贴到文本框。', 'error');
      }
    });

    document.getElementById('clear').addEventListener('click', () => {
      document.getElementById('url').value = '';
      document.getElementById('html').value = '';
      titleEl.textContent = '等待生成';
      countEl.textContent = '暂无标注草稿';
      copyAllBtn.disabled = true;
      cardsEl.innerHTML = '<div class="empty">输入 URL 后点击生成。<br />如果网络抓取失败，可粘贴 HTML 再生成。</div>';
      lastPayload = null;
      setStatus('');
    });

    cardsEl.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-copy]');
      if (!button || !lastPayload) return;
      const item = lastPayload.annotations[Number(button.dataset.copy)];
      if (item.fullText) {
        copyText(`类型：${item.type}\n标注范围：全文\n标注内容：${item.content}`);
      } else {
        copyText(`类型：${item.type}\n建议选中文本：${item.selectedText}\n标注内容：${item.content}`);
      }
    });

    copyAllBtn.addEventListener('click', () => {
      if (lastPayload) copyText(formatAll(lastPayload));
    });

    loadBrowserImport(false).catch(() => {});
    setInterval(() => {
      loadBrowserImport(false).catch(() => {});
    }, 2000);
  </script>
</body>
</html>
"""


IMPORT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CSDN 标注工具导入</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f6f7fb;
      color: #1f2937;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
    }
    main {
      width: min(520px, calc(100vw - 32px));
      background: #fff;
      border: 1px solid #d8dee9;
      border-radius: 8px;
      padding: 22px;
      line-height: 1.7;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 18px;
      font-weight: 650;
    }
    .status { color: #64748b; }
    .ok { color: #047857; }
    .error { color: #b91c1c; }
  </style>
</head>
<body>
  <main>
    <h1>正在导入当前网页 HTML</h1>
    <div id="status" class="status">请保持这个窗口打开，导入完成后会自动回到工具页面。</div>
  </main>
  <script>
    const statusEl = document.getElementById('status');
    let imported = false;

    function setStatus(message, kind = '') {
      statusEl.textContent = message;
      statusEl.className = 'status ' + kind;
    }

    window.addEventListener('message', async (event) => {
      if (imported) return;
      const data = event.data || {};
      if (!data || data.source !== 'csdn-annotation-tool' || !data.html) return;
      imported = true;
      setStatus('已收到网页 HTML，正在写入本地工具...');
      try {
        const response = await fetch('/api/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data)
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || '导入失败');
        setStatus('导入完成，正在返回工具页面...', 'ok');
        setTimeout(() => { location.href = '/?imported=1'; }, 700);
      } catch (error) {
        imported = false;
        setStatus(error.message, 'error');
      }
    });

    setTimeout(() => {
      if (!imported) {
        setStatus('还没有收到网页 HTML。请回到 CSDN 页面重新点击导入书签。');
      }
    }, 8000);
  </script>
</body>
</html>
"""


class AnnotationHandler(BaseHTTPRequestHandler):
    server_version = "CSDNAnnotationUI/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_html(INDEX_HTML)
            return
        if path == "/import":
            self._send_html(IMPORT_HTML)
            return
        if path == "/api/import/latest":
            query = urllib.parse.parse_qs(parsed.query)
            since = query.get("since", [""])[0]
            self._send_json(latest_browser_import(since=since))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path not in {"/api/generate", "/api/import"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            if path == "/api/generate":
                result = generate(payload)
                self._send_json(result)
                return
            if path == "/api/import":
                save_browser_import(payload)
                self._send_json({"ok": True})
                return
        except Exception as exc:
            log_exception(exc)
            self._send_json({"error": client_error_message(exc)}, status=error_status(exc))

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        except Exception:
            pass

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def save_browser_import(payload: Dict[str, Any]) -> None:
    page_html = str(payload.get("html") or "")
    if not page_html.strip():
        raise ValueError("浏览器导入失败：页面 HTML 为空。")
    data = {
        "url": str(payload.get("url") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "html": page_html,
        "receivedAt": datetime.now().isoformat(timespec="seconds"),
    }
    with BROWSER_IMPORT_LOCK:
        BROWSER_IMPORT.clear()
        BROWSER_IMPORT.update(data)
    log_event(f"browser import received url={data['url'] or '<unknown>'} bytes={len(page_html)}")


def latest_browser_import(since: str = "") -> Dict[str, Any]:
    with BROWSER_IMPORT_LOCK:
        data = dict(BROWSER_IMPORT)
    if since and data.get("receivedAt") == since:
        return {
            "unchanged": True,
            "url": data.get("url", ""),
            "title": data.get("title", ""),
            "receivedAt": data.get("receivedAt", ""),
        }
    return data


def is_csdn_waf_error(exc: Exception) -> bool:
    return (
        isinstance(exc, urllib.error.HTTPError)
        and exc.code == 521
        and exc.headers.get("Server", "").upper() == "WAF"
    )


def normalize_article_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )


def latest_matching_import(url: str) -> Dict[str, Any]:
    with BROWSER_IMPORT_LOCK:
        data = dict(BROWSER_IMPORT)
    page_html = str(data.get("html") or "")
    if not page_html.strip():
        return {}
    imported_url = str(data.get("url") or "").strip()
    if imported_url and normalize_article_url(imported_url) != normalize_article_url(url):
        return {}
    return data


def generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    raw_html = str(payload.get("html") or "")
    insecure = bool(payload.get("insecure"))
    use_codex = bool(payload.get("useCodex"))
    full_text = bool(payload.get("fullText"))
    limit = max(1, min(int(payload.get("max") or 5), 5))
    log_event(f"generate start useCodex={use_codex} fullText={full_text} limit={limit} url={url or '<html>'}")

    if raw_html.strip():
        page_html = raw_html
    elif url:
        log_event("fetch html start")
        try:
            page_html = fetch_html(url, insecure=insecure)
            log_event(f"fetch html done bytes={len(page_html)}")
        except Exception as exc:
            imported = latest_matching_import(url) if is_csdn_waf_error(exc) else {}
            if not imported:
                raise
            page_html = str(imported["html"])
            log_event(
                "fetch html blocked by CSDN WAF; "
                f"using browser import receivedAt={imported.get('receivedAt', '')} bytes={len(page_html)}"
            )
    else:
        raise ValueError("请填写文章 URL 或粘贴 HTML。")

    article = parse_article(page_html)
    drafts = [build_rule_full_text_draft(article)] if full_text else build_drafts(article, limit)
    log_event(f"article parsed title={article.title!r} paragraphs={len(article.paragraphs)} ruleDrafts={len(drafts)}")
    provider = "规则生成"
    warning = None
    if use_codex:
        try:
            codex_drafts = build_codex_full_text_draft(article) if full_text else build_codex_drafts(article, limit)
            if codex_drafts:
                drafts = codex_drafts
                provider = "本机 Codex"
            else:
                warning = "Codex 没有返回可用草稿，已回退到规则生成。"
        except Exception as exc:
            warning = f"Codex 增强失败，已回退到规则生成：{exc}"
            log_event(warning)

    result = {
        "title": article.title,
        "url": url,
        "provider": provider,
        "annotations": [
            {
                "type": draft.annotation_type,
                "selectedText": draft.selected_text,
                "content": draft.content,
                "reason": draft.reason,
                "fullText": full_text or not draft.selected_text,
            }
            for draft in drafts
        ],
    }
    if warning:
        result["warning"] = warning
    log_event(f"generate done provider={provider} annotations={len(result['annotations'])}")
    return result


def build_codex_drafts(article, limit: int):
    paragraphs = build_codex_paragraphs(article.paragraphs)
    if not paragraphs:
        return []

    prompt = build_codex_prompt(article.title, paragraphs, limit)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as fp:
        output_path = fp.name
    try:
        command = build_codex_command(output_path)
        log_event(f"codex start paragraphs={len(paragraphs)} timeout={CODEX_TIMEOUT_SECONDS}s")
        completed = run_codex_command(
            command,
            prompt,
            timeout=CODEX_TIMEOUT_SECONDS,
        )
        if completed["returncode"] != 0:
            tail = "\n".join(completed["lines"][-8:]).strip()
            raise RuntimeError(tail[-500:] or f"codex exec 退出码 {completed['returncode']}")
        with open(output_path, "r", encoding="utf-8", errors="replace") as fp:
            raw = fp.read()
        log_event(f"codex final message bytes={len(raw)}")
        payload = parse_json_from_text(raw)
        items = payload.get("annotations") if isinstance(payload, dict) else payload
        drafts = normalize_codex_drafts(items, paragraphs, limit)
        log_event(f"codex parsed drafts={len(drafts)}")
        return drafts
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


def build_rule_full_text_draft(article) -> AnnotationDraft:
    return AnnotationDraft(
        annotation_type="内容质量",
        selected_text="",
        content=(
            "全文整体结构较清晰，围绕主题给出了较完整的说明。建议进一步补充适用边界、版本环境和可复现结果，"
            "让读者能更准确判断内容在实际项目中的参考价值。"
        ),
        reason="针对全文的整体质量、完整性和可复现性评价",
    )


def build_codex_full_text_draft(article) -> List[AnnotationDraft]:
    paragraphs = build_codex_paragraphs(article.paragraphs)
    if not paragraphs:
        return []
    prompt = build_codex_full_text_prompt(article.title, paragraphs)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as fp:
        output_path = fp.name
    try:
        command = build_codex_command(output_path)
        log_event(f"codex full-text start paragraphs={len(paragraphs)} timeout={CODEX_TIMEOUT_SECONDS}s")
        completed = run_codex_command(command, prompt, timeout=CODEX_TIMEOUT_SECONDS)
        if completed["returncode"] != 0:
            tail = "\n".join(completed["lines"][-8:]).strip()
            raise RuntimeError(tail[-500:] or f"codex exec 退出码 {completed['returncode']}")
        with open(output_path, "r", encoding="utf-8", errors="replace") as fp:
            raw = fp.read()
        log_event(f"codex full-text final message bytes={len(raw)}")
        payload = parse_json_from_text(raw)
        item = payload.get("annotation") if isinstance(payload, dict) else None
        draft = normalize_codex_full_text_draft(item)
        log_event(f"codex full-text parsed drafts={1 if draft else 0}")
        return [draft] if draft else []
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


def run_codex_command(command: List[str], prompt: str, timeout: int) -> Dict[str, Any]:
    lines: List[str] = []
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def reader(name: str, pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                line = line.rstrip()
                if line:
                    lines.append(f"{name}: {line}")
                    log_event(f"codex {name}: {line}")
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        log_event(f"codex timeout after {timeout}s")
        raise RuntimeError(f"Codex 超过 {timeout} 秒未返回，已终止。") from exc

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    log_event(f"codex finished returncode={returncode}")
    return {"returncode": returncode, "lines": lines}


def build_codex_paragraphs(paragraphs: List[str]) -> List[Dict[str, Any]]:
    result = []
    for index, text in enumerate(paragraphs, 1):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(text) < 20:
            continue
        if len(text) > 900:
            text = text[:900].rstrip() + "..."
        result.append({"id": index, "text": text})
        if len(result) >= 45:
            break
    return result


def build_codex_command(output_path: str) -> List[str]:
    codex_path = (
        shutil.which("codex.cmd")
        or shutil.which("codex.exe")
        or shutil.which("codex.ps1")
        or shutil.which("codex")
    )
    if not codex_path:
        raise RuntimeError("未找到本机 codex 命令")

    args = [
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--output-last-message",
        output_path,
        "-",
    ]
    if codex_path.lower().endswith(".ps1"):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("找到 codex.ps1，但未找到 powershell")
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            codex_path,
            *args,
        ]
    return [codex_path, *args]


def build_codex_prompt(title: str, paragraphs: List[Dict[str, Any]], limit: int) -> str:
    return (
        "你是 CSDN 专家标注助手。请阅读文章段落，自己选择最值得专家标注的位置，并生成标注草稿。\n"
        "要求：\n"
        "1. 每条标注必须选择一个 paragraphId，并从该段落中截取一段连续原文作为 selectedText，不要改写原文。\n"
        "2. type 只能是：运行环境 & 效果、适用场景、补充案例、内容质量。\n"
        "3. content 必须是中文纯文字，20 到 200 字，客观、专业、可直接提交审核。\n"
        f"4. 最多返回 {limit} 条，尽量覆盖不同标注类型。\n"
        "5. selectedText 最长 500 字，优先选择有技术判断、环境依赖、适用边界、实践经验或可能需要补充说明的段落。\n"
        "6. 只返回 JSON，不要 Markdown，不要解释。\n"
        "JSON 格式："
        '{"annotations":[{"paragraphId":1,"type":"内容质量","selectedText":"...","content":"...","reason":"..."}]}'
        "\n\n"
        f"文章标题：{title}\n"
        f"文章段落：\n{json.dumps(paragraphs, ensure_ascii=False, indent=2)}"
    )


def build_codex_full_text_prompt(title: str, paragraphs: List[Dict[str, Any]]) -> str:
    return (
        "你是 CSDN 专家标注助手。请基于全文生成一条“全文标注”草稿。\n"
        "要求：\n"
        "1. 不需要 selectedText，也不要选择具体段落。\n"
        "2. type 只能是：内容质量、适用场景、补充案例、运行环境 & 效果。优先选择内容质量。\n"
        "3. content 必须是中文纯文字，20 到 200 字，客观、专业、可直接提交审核。\n"
        "4. 从全文角度评价文章的完整性、准确性、适用边界、可复现性或补充价值。\n"
        "5. 只返回 JSON，不要 Markdown，不要解释。\n"
        "JSON 格式："
        '{"annotation":{"type":"内容质量","content":"...","reason":"..."}}'
        "\n\n"
        f"文章标题：{title}\n"
        f"文章段落：\n{json.dumps(paragraphs, ensure_ascii=False, indent=2)}"
    )


def parse_json_from_text(raw: str):
    text = raw.strip()
    if not text:
        raise ValueError("Codex 输出为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("无法从 Codex 输出中解析 JSON")


def normalize_codex_drafts(items, paragraphs: List[Dict[str, Any]], limit: int):
    if not isinstance(items, list):
        raise ValueError("Codex JSON 中 annotations 不是数组")
    allowed_types = {"运行环境 & 效果", "适用场景", "补充案例", "内容质量"}
    paragraph_by_id = {int(item["id"]): item["text"] for item in paragraphs}
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        paragraph_id = safe_int(item.get("paragraphId"))
        annotation_type = str(item.get("type") or "").strip()
        selected_text = str(item.get("selectedText") or "").strip()
        content = str(item.get("content") or "").strip()
        reason = str(item.get("reason") or "Codex 生成").strip()
        if annotation_type not in allowed_types:
            continue
        paragraph_text = paragraph_by_id.get(paragraph_id)
        if not paragraph_text:
            continue
        selected_text = normalize_selected_text(selected_text, paragraph_text)
        if not selected_text:
            continue
        if not (20 <= len(content) <= 200):
            continue
        result.append(AnnotationDraft(annotation_type, selected_text, content, reason))
        if len(result) >= limit:
            break
    return result


def normalize_codex_full_text_draft(item) -> AnnotationDraft:
    allowed_types = {"运行环境 & 效果", "适用场景", "补充案例", "内容质量"}
    if not isinstance(item, dict):
        raise ValueError("Codex JSON 中 annotation 不是对象")
    annotation_type = str(item.get("type") or "内容质量").strip()
    content = str(item.get("content") or "").strip()
    reason = str(item.get("reason") or "Codex 全文标注").strip()
    if annotation_type not in allowed_types:
        annotation_type = "内容质量"
    if not (20 <= len(content) <= 200):
        raise ValueError("Codex 全文标注内容长度不符合 20-200 字要求")
    return AnnotationDraft(annotation_type, "", content, reason)


def safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return -1


def normalize_selected_text(selected_text: str, paragraph_text: str) -> str:
    selected_text = re.sub(r"\s+", " ", selected_text or "").strip()
    paragraph_text = re.sub(r"\s+", " ", paragraph_text or "").strip()
    if not selected_text or not paragraph_text:
        return ""
    if selected_text in paragraph_text:
        return selected_text[:500]
    compact_selected = re.sub(r"\s+", "", selected_text)
    compact_paragraph = re.sub(r"\s+", "", paragraph_text)
    if compact_selected and compact_selected in compact_paragraph:
        return selected_text[:500]
    return ""


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start local CSDN annotation UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), AnnotationHandler)
    safe_console_print(f"CSDN 标注草稿界面已启动: http://{args.host}:{args.port}")
    safe_console_print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_console_print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
