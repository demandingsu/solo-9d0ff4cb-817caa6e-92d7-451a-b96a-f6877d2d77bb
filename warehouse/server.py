#!/usr/bin/env python3
"""食品仓库质控端：隔离货物召回取件预约（纯标准库，离线可运行）。

核心保证：
  1. 按交接单号去重：同号在页面上只能创建一次，重复点击/并发请求只读取既有记录；
  2. 生产商同号至多受理一次：任何“结果不明”的提交只置为待确认，绝不二次提交；
  3. 回执中断后按交接单号查询：查到 -> 展示原预约号；明确未受理 -> 允许重试；
     查询失败 -> 保持待确认、不再提交；
  4. 应用重启后自动核对全部待确认/提交中交接单。
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from httpclient import HttpTransportError, http_get, http_post_form, parse_json
from recall_parser import RecallPageError, build_submission_payload, parse_recall_page

DB_PATH = os.environ.get("WH_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "warehouse.db"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "12"))
RECONCILE_BACKOFF = int(os.environ.get("RECONCILE_BACKOFF", "10"))  # 查询失败后的最小重试间隔（秒）

# 状态机
PENDING = "PENDING_CONFIRMATION"   # 待确认：已提交但结果不明，或查询失败，禁止再次提交
CONFIRMED = "CONFIRMED"            # 已确认：拿到生产商预约号
NOT_ACCEPTED = "NOT_ACCEPTED"      # 明确未受理：允许重新提交
SUBMITTING = "SUBMITTING"          # 提交中（进程崩溃重启后等同待确认处理）

STATUS_LABELS = {
    PENDING: ("待确认", "#9a6700", "#fff8c5"),
    CONFIRMED: ("已确认", "#1a7f37", "#dafbe1"),
    NOT_ACCEPTED: ("明确未受理", "#b42318", "#ffebe9"),
    SUBMITTING: ("提交中", "#0969da", "#ddf4ff"),
}

_lock = threading.RLock()
_tls = threading.local()


def db() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _tls.conn = conn
        _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handover_no TEXT NOT NULL UNIQUE,
            detail_url TEXT NOT NULL,
            box_count INTEGER NOT NULL,
            pickup_date TEXT NOT NULL,
            status TEXT NOT NULL,
            appointment_no TEXT DEFAULT '',
            submit_url TEXT DEFAULT '',
            lookup_url TEXT DEFAULT '',
            announcement_id TEXT DEFAULT '',
            source_title TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_reconcile_at REAL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER,
            handover_no TEXT,
            event TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # 崩溃恢复：重启后残留的 SUBMITTING 一律视为待确认
    conn.execute(
        f"UPDATE appointments SET status=?, updated_at=? WHERE status=?",
        (PENDING, _now(), SUBMITTING),
    )
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(appointment_id: int | None, handover_no: str, event: str, detail: str = "") -> None:
    conn = db()
    conn.execute(
        "INSERT INTO event_log (appointment_id, handover_no, event, detail, created_at) VALUES (?,?,?,?,?)",
        (appointment_id, handover_no, event, detail, _now()),
    )
    conn.commit()


def _set_status(row_id: int, status: str, **fields) -> None:
    conn = db()
    cols = ["status=?", "updated_at=?", "version=version+1"]
    vals: list = [status, _now()]
    for key, val in fields.items():
        cols.append(f"{key}=?")
        vals.append(val)
    vals.append(row_id)
    conn.execute(f"UPDATE appointments SET {', '.join(cols)} WHERE id=?", vals)
    conn.commit()


# ---------------------------------------------------------------- 生产商交互

def fetch_form(detail_url: str) -> dict:
    """拉取并解析召回详情页，返回表单与查询入口信息。"""
    status, body = http_get(detail_url)
    if status != 200:
        raise RecallPageError(f"详情页返回 HTTP {status}")
    info = parse_recall_page(body.decode("utf-8"), detail_url)
    # 公告标题（粗解析，仅用于页面展示“公告来源”）
    title = ""
    text = body.decode("utf-8", errors="replace")
    start = text.find("<title>")
    if start >= 0:
        end = text.find("</title>", start)
        title = text[start + 7: end].strip() if end > start else ""
    info["source_title"] = title
    return info


def submit_once(record: sqlite3.Row, form_info: dict | None = None) -> None:
    """执行一次提交，并严格按回执情况推进状态。任何结果不明都只置待确认。"""
    conn = db()
    if form_info is None:
        form_info = {
            "submit_url": record["submit_url"],
            "lookup_url": record["lookup_url"],
            "fields": [],
            "source_title": record["source_title"],
        }
    payload = build_submission_payload(
        form_info["fields"],
        {
            "handover_no": record["handover_no"],
            "box_count": str(record["box_count"]),
            "pickup_date": record["pickup_date"],
            "announcement_id": record["announcement_id"],
        },
    )
    log_event(record["id"], record["handover_no"], "SUBMIT", f"POST {form_info['submit_url']}")
    try:
        status, resp_body = http_post_form(form_info["submit_url"], payload)
    except HttpTransportError as exc:
        # 连接/超时：不知道是否受理 -> 待确认，等待核对
        _set_status(record["id"], PENDING, note=f"提交传输失败（结果不明）：{exc}")
        log_event(record["id"], record["handover_no"], "TRANSPORT_ERROR", str(exc))
        return

    if 200 <= status < 300:
        try:
            data = parse_json(resp_body)
        except (ValueError, UnicodeDecodeError):
            data = {}
        if data.get("accepted") and data.get("appointment_no"):
            _set_status(
                record["id"],
                CONFIRMED,
                appointment_no=data["appointment_no"],
                note=("同号重复受理请求，返回原预约号" if data.get("duplicate") else "受理成功"),
            )
            log_event(record["id"], record["handover_no"], "CONFIRMED", data["appointment_no"])
            return
        # 2xx 但没有预约号：语义不明，按待确认处理
        _set_status(record["id"], PENDING, note=f"响应缺少预约号（HTTP {status}），转待确认核对")
        log_event(record["id"], record["handover_no"], "AMBIGUOUS_RESPONSE", resp_body[:200].decode("utf-8", "replace"))
        return

    # 4xx/5xx/空回执（含 502 断连）：可能已受理 -> 待确认，禁止重复提交
    snippet = resp_body[:200].decode("utf-8", "replace") if resp_body else "<空响应体>"
    _set_status(record["id"], PENDING, note=f"回执中断/异常 HTTP {status}：{snippet}")
    log_event(record["id"], record["handover_no"], "RECEIPT_INTERRUPTED", f"HTTP {status} {snippet}")


def lookup_result(record: sqlite3.Row) -> str:
    """按交接单号查询受理结果。

    返回：CONFIRMED（查到受理）/ NOT_ACCEPTED（明确未受理）/ PENDING（查询失败，保持待确认）。
    """
    url = record["lookup_url"]
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}handover_no={urllib.parse.quote(record['handover_no'])}"
    log_event(record["id"], record["handover_no"], "LOOKUP", full)
    try:
        status, body = http_get(full)
    except HttpTransportError as exc:
        _touch_reconcile(record["id"], note_append=f"；查询传输失败：{exc}")
        log_event(record["id"], record["handover_no"], "LOOKUP_FAILED", str(exc))
        return PENDING

    if not (200 <= status < 300):
        _touch_reconcile(record["id"], note_append=f"；查询返回 HTTP {status}，保持待确认")
        log_event(record["id"], record["handover_no"], "LOOKUP_FAILED", f"HTTP {status}")
        return PENDING

    try:
        data = parse_json(body)
    except (ValueError, UnicodeDecodeError) as exc:
        _touch_reconcile(record["id"], note_append=f"；查询响应无法解析：{exc}")
        log_event(record["id"], record["handover_no"], "LOOKUP_FAILED", "invalid json")
        return PENDING

    if data.get("accepted") and data.get("appointment_no"):
        _set_status(
            record["id"],
            CONFIRMED,
            appointment_no=data["appointment_no"],
            note="按交接单号核对到受理记录，展示原预约号",
            last_reconcile_at=time.time(),
        )
        log_event(record["id"], record["handover_no"], "LOOKUP_CONFIRMED", data["appointment_no"])
        return CONFIRMED

    if data.get("accepted") is False:
        # 明确未受理，才允许后续重试
        _set_status(
            record["id"],
            NOT_ACCEPTED,
            note="生产商明确未受理该交接单号，可重新提交",
            last_reconcile_at=time.time(),
        )
        log_event(record["id"], record["handover_no"], "LOOKUP_NOT_ACCEPTED", "")
        return NOT_ACCEPTED

    _touch_reconcile(record["id"], note_append="；查询响应语义不明，保持待确认")
    log_event(record["id"], record["handover_no"], "LOOKUP_FAILED", "ambiguous payload")
    return PENDING


def _touch_reconcile(row_id: int, note_append: str = "") -> None:
    conn = db()
    row = conn.execute("SELECT note FROM appointments WHERE id=?", (row_id,)).fetchone()
    note = (row["note"] or "") + note_append
    conn.execute(
        "UPDATE appointments SET last_reconcile_at=?, note=?, updated_at=?, version=version+1 WHERE id=?",
        (time.time(), note[-500:], _now(), row_id),
    )
    conn.commit()


# ---------------------------------------------------------------- 后台核对

def reconcile_pending() -> int:
    """核对所有待确认交接单，返回本次处理条数。"""
    conn = db()
    rows = conn.execute(
        f"SELECT * FROM appointments WHERE status IN (?, ?) ORDER BY id", (PENDING, SUBMITTING)
    ).fetchall()
    now = time.time()
    handled = 0
    for row in rows:
        if now - (row["last_reconcile_at"] or 0) < RECONCILE_BACKOFF:
            continue
        with _lock:
            fresh = conn.execute("SELECT * FROM appointments WHERE id=?", (row["id"],)).fetchone()
            if fresh["status"] not in (PENDING, SUBMITTING):
                continue  # 已被手动核对/其他线程处理
            lookup_result(fresh)
            handled += 1
    return handled


def _reconciler_loop() -> None:
    while True:
        time.sleep(RECONCILE_INTERVAL)
        try:
            reconcile_pending()
        except Exception as exc:  # 后台线程不能死
            print(f"[warehouse] reconciler error: {exc!r}")


# ---------------------------------------------------------------- 页面

CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f4f6f9;color:#1f2328}
header{background:#0f2d4a;color:#fff;padding:16px 28px}
main{max-width:1080px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{margin-top:0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #d8dee4;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#eef2f6}
label{display:block;margin:10px 0;font-size:14px}
input{padding:8px;border:1px solid #afb8c1;border-radius:6px;width:320px;max-width:100%}
button,.btn{appearance:none;border:0;border-radius:6px;padding:8px 14px;background:#1f6feb;color:#fff;cursor:pointer;text-decoration:none;display:inline-block;margin:3px 6px 3px 0;font-size:13px}
button.gray{background:#57606a}
button.orange{background:#bf570a}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700}
.muted{color:#656d76;font-size:13px}
.flash{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px}
.flash.ok{background:#dafbe1;color:#1a7f37;border:1px #aceebb solid}
.flash.err{background:#ffebe9;color:#b42318;border:1px #ffabaf solid}
.flash.warn{background:#fff8c5;color:#9a6700;border:1px #e3d27a solid}
code{background:#eef1f4;padding:2px 6px;border-radius:4px}
.pill{font-family:monospace}
"""


def _badge(status: str) -> str:
    label, fg, bg = STATUS_LABELS[status]
    return f'<span class="badge" style="color:{fg};background:{bg}">{label}</span>'


def _esc(val) -> str:
    return html.escape(str(val if val is not None else ""))


def list_page(flash: tuple[str, str] | None = None) -> bytes:
    conn = db()
    rows = conn.execute("SELECT * FROM appointments ORDER BY id DESC").fetchall()
    trs = []
    for r in rows:
        actions = []
        if r["status"] in (PENDING, SUBMITTING):
            actions.append(f'<form method="post" action="/appointments/{r["id"]}/reconcile" style="display:inline"><button class="orange">立即核对受理结果</button></form>')
        if r["status"] == NOT_ACCEPTED:
            actions.append(f'<form method="post" action="/appointments/{r["id"]}/retry" style="display:inline"><button>重新提交预约</button></form>')
        if r["status"] == CONFIRMED:
            actions.append('<span class="muted">已受理，不可重复下单</span>')
        trs.append(f"""
        <tr>
          <td class="pill">{_esc(r['handover_no'])}</td>
          <td>{_badge(r['status'])}</td>
          <td class="pill">{_esc(r['appointment_no']) or '<span class="muted">—</span>'}</td>
          <td>{_esc(r['box_count'])}</td>
          <td>{_esc(r['pickup_date'])}</td>
          <td><a href="{_esc(r['detail_url'])}" target="_blank" rel="noopener">{_esc(r['source_title'] or r['announcement_id'] or '召回详情页')}</a>
              <div class="muted">{_esc(r['detail_url'])}</div></td>
          <td>{''.join(actions)}
              <div class="muted" style="margin-top:6px">{_esc(r['note'])}</div></td>
        </tr>""")
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{_esc(msg)}</div>'
    body = f"""
    {flash_html}
    <div class="card">
      <h2>新建取件预约</h2>
      <form method="post" action="/appointments">
        <label>召回详情 URL <input name="detail_url" required placeholder="http://localhost:8081/recall/recall-2026-001"></label>
        <label>交接单号 <input name="handover_no" required maxlength="40" placeholder="HO-20261111-0007"></label>
        <label>箱数 <input name="box_count" type="number" min="1" max="99999" required></label>
        <label>取件日期 <input name="pickup_date" type="date" required value="{date.today().isoformat()}"></label>
        <button type="submit">提交预约</button>
      </form>
      <p class="muted">同一交接单号只能下单一次；提交超时或回执中断时系统自动转为“待确认”并按交接单号核对，不会重复提交。</p>
    </div>
    <div class="card">
      <h2>预约记录（{len(rows)}）</h2>
      <table><thead><tr>
        <th>交接单号</th><th>预约状态</th><th>预约号</th><th>箱数</th><th>取件日期</th><th>公告来源</th><th>操作 / 说明</th>
      </tr></thead><tbody>
      {''.join(trs) or '<tr><td colspan="7" class="muted">暂无预约记录</td></tr>'}
      </tbody></table>
      <p class="muted" style="margin-top:12px">页面每 10 秒自动刷新，以便展示后台核对结果。</p>
    </div>
    <script>setInterval(()=>location.reload(),10000);</script>"""
    return _page("取件预约工作台", body)


def detail_page(row_id: int) -> bytes | None:
    conn = db()
    row = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
    if not row:
        return None
    logs = conn.execute(
        "SELECT * FROM event_log WHERE appointment_id=? ORDER BY id DESC LIMIT 30", (row_id,)
    ).fetchall()
    log_html = "".join(
        f"<tr><td>{_esc(l['created_at'])}</td><td>{_esc(l['event'])}</td><td class='muted'>{_esc(l['detail'])}</td></tr>"
        for l in logs
    )
    actions = []
    if row["status"] in (PENDING, SUBMITTING):
        actions.append(f'<form method="post" action="/appointments/{row_id}/reconcile"><button class="orange">立即核对受理结果</button></form>')
    if row["status"] == NOT_ACCEPTED:
        actions.append(f'<form method="post" action="/appointments/{row_id}/retry"><button>重新提交预约</button></form>')
    body = f"""
    <div class="card">
      <h2>交接单 {_esc(row['handover_no'])}</h2>
      <table>
        <tr><th>预约状态</th><td>{_badge(row['status'])}</td></tr>
        <tr><th>预约号</th><td class="pill">{_esc(row['appointment_no']) or '—'}</td></tr>
        <tr><th>箱数 / 取件日期</th><td>{_esc(row['box_count'])} 箱 ｜ {_esc(row['pickup_date'])}</td></tr>
        <tr><th>公告来源</th><td><a href="{_esc(row['detail_url'])}" target="_blank" rel="noopener">{_esc(row['source_title'] or row['announcement_id'])}</a><br>
            <code>{_esc(row['detail_url'])}</code></td></tr>
        <tr><th>提交 / 查询入口</th><td><code>{_esc(row['submit_url'])}</code><br><code>{_esc(row['lookup_url'])}</code></td></tr>
        <tr><th>说明</th><td class="muted">{_esc(row['note'])}</td></tr>
        <tr><th>更新时间</th><td>{_esc(row['updated_at'])}（版本 {row['version']}）</td></tr>
      </table>
      <p style="margin-top:14px">{''.join(actions)}
         <a class="btn gray" href="/" style="background:#57606a">← 返回列表</a></p>
    </div>
    <div class="card">
      <h2>事件日志</h2>
      <table><thead><tr><th>时间</th><th>事件</th><th>详情</th></tr></thead>
      <tbody>{log_html or '<tr><td colspan=3 class=muted>暂无</td></tr>'}</tbody></table>
    </div>"""
    return _page(f"交接单 {row['handover_no']}", body)


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} - 仓库质控取件预约</title>
<style>{CSS}</style></head>
<body>
<header><h1 style="margin:0;font-size:19px">食品仓库质控 · 隔离货物取件预约工作台</h1></header>
<main>{body}</main>
</body></html>""".encode("utf-8")


# ---------------------------------------------------------------- HTTP 路由

class Handler(BaseHTTPRequestHandler):
    server_version = "WarehouseQC/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[warehouse] {self.address_string()} {fmt % args}")

    def _html(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[0].strip() for k, v in parsed.items()}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        flash = self._read_flash()
        if path == "/":
            return self._html(200, list_page(flash))
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path.startswith("/appointments/") and path.endswith("/events") is False:
            try:
                row_id = int(path.split("/")[2])
            except (IndexError, ValueError):
                return self._html(404, b"not found")
            page = detail_page(row_id)
            return self._html(200 if page else 404, page or b"not found")
        return self._html(404, b"not found")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/appointments":
                return self._create()
            if path.endswith("/reconcile"):
                return self._reconcile(int(path.split("/")[2]))
            if path.endswith("/retry"):
                return self._retry(int(path.split("/")[2]))
            return self._html(404, b"not found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---- flash message（PRG 模式，cookie 简单传递）----
    def _read_flash(self) -> tuple[str, str] | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("flash="):
                raw = urllib.parse.unquote(part.split("=", 1)[1])
                kind, _, msg = raw.partition("|")
                self._clear_flash = True  # 渲染页面时顺手清掉一次性 cookie
                return kind, msg
        return None

    def _flash_cookie(self, kind: str, msg: str) -> str:
        return f'flash={urllib.parse.quote(kind + "|" + msg)}; Path=/; Max-Age=30; HttpOnly'

    def _clear_flash_cookie(self) -> str:
        return "flash=; Path=/; Max-Age=0"

    def _redirect_flash(self, location: str, kind: str, msg: str) -> None:
        self._suppress_clearing = True
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", self._flash_cookie(kind, msg))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self) -> None:
        # 普通响应清理一次性 flash cookie；303 跳转响应本身带新 cookie，不清理
        if getattr(self, "_clear_flash", False) and not getattr(self, "_suppress_clearing", False):
            self.send_header("Set-Cookie", self._clear_flash_cookie())
        super().end_headers()

    # ---------------- 业务动作 ----------------
    def _create(self) -> None:
        form = self._form()
        detail_url = form.get("detail_url", "")
        handover_no = form.get("handover_no", "")
        box_count_raw = form.get("box_count", "")
        pickup_date = form.get("pickup_date", "")

        if not detail_url.startswith(("http://", "https://")):
            return self._redirect_flash("/", "err", "召回详情 URL 必须以 http:// 或 https:// 开头")
        if not handover_no:
            return self._redirect_flash("/", "err", "交接单号不能为空")
        try:
            box_count = int(box_count_raw)
            if box_count <= 0:
                raise ValueError
        except ValueError:
            return self._redirect_flash("/", "err", "箱数必须是正整数")
        try:
            datetime.strptime(pickup_date, "%Y-%m-%d")
        except ValueError:
            return self._redirect_flash("/", "err", "取件日期格式应为 YYYY-MM-DD")

        conn = db()
        # 第一重防护：UNIQUE 约束；第二重：进程锁串行化同号请求
        with _lock:
            existing = conn.execute(
                "SELECT * FROM appointments WHERE handover_no=?", (handover_no,)
            ).fetchone()
            if existing is not None:
                label = STATUS_LABELS[existing["status"]][0]
                return self._redirect_flash(
                    "/",
                    "warn",
                    f"交接单号 {handover_no} 已存在（当前状态：{label}），未重复下单。请到列表中核对或重试。",
                )

            # 先解析生产商详情页，找到预约表单与查询入口
            try:
                form_info = fetch_form(detail_url)
            except (RecallPageError, HttpTransportError) as exc:
                return self._redirect_flash("/", "err", f"无法读取召回详情页：{exc}")

            cur = conn.execute(
                """INSERT INTO appointments
                   (handover_no, detail_url, box_count, pickup_date, status, submit_url, lookup_url,
                    announcement_id, source_title, note, created_at, updated_at, last_reconcile_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    handover_no, detail_url, box_count, pickup_date, SUBMITTING,
                    form_info["submit_url"], form_info["lookup_url"],
                    next((f["value"] for f in form_info["fields"] if f["name"] == "announcement_id"), ""),
                    form_info.get("source_title", ""),
                    "已从详情页定位生产商预约表单",
                    _now(), _now(), 0.0,
                ),
            )
            row_id = cur.lastrowid
            log_event(row_id, handover_no, "CREATE", f"detail={detail_url} boxes={box_count} date={pickup_date}")
            conn.commit()
            row = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
            submit_once(row, form_info)
            fresh = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()

        if fresh["status"] == CONFIRMED:
            return self._redirect_flash(
                "/", "ok", f"预约成功：交接单 {handover_no} 的预约号为 {fresh['appointment_no']}"
            )
        return self._redirect_flash(
            "/", "warn",
            f"交接单 {handover_no} 回执中断或结果不明，已置为“待确认”；系统将自动按交接单号核对，不会重复提交。",
        )

    def _reconcile(self, row_id: int) -> None:
        conn = db()
        with _lock:
            row = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
            if not row:
                return self._redirect_flash("/", "err", "记录不存在")
            if row["status"] not in (PENDING, SUBMITTING):
                return self._redirect_flash("/", "warn", f"当前状态为「{STATUS_LABELS[row['status']][0]}」，无需核对")
            result = lookup_result(row)
        if result == CONFIRMED:
            fresh = db().execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
            return self._redirect_flash("/", "ok", f"核对成功：原预约号 {fresh['appointment_no']}（未产生重复预约）")
        if result == NOT_ACCEPTED:
            return self._redirect_flash("/", "warn", "生产商明确未受理该交接单，现在可以重新提交")
        return self._redirect_flash("/", "err", "受理结果查询失败：保持待确认状态，不会重复提交，请稍后再试")

    def _retry(self, row_id: int) -> None:
        conn = db()
        with _lock:
            row = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
            if not row:
                return self._redirect_flash("/", "err", "记录不存在")
            if row["status"] != NOT_ACCEPTED:
                return self._redirect_flash("/", "err", "只有“明确未受理”的交接单才允许重新提交")
            # 重新拉取详情页表单，再提交一次
            try:
                form_info = fetch_form(row["detail_url"])
            except (RecallPageError, HttpTransportError) as exc:
                return self._redirect_flash("/", "err", f"无法读取召回详情页：{exc}")
            _set_status(row_id, SUBMITTING, submit_url=form_info["submit_url"],
                        lookup_url=form_info["lookup_url"], note="明确未受理后重新提交")
            log_event(row_id, row["handover_no"], "RETRY", "re-submit after confirmed not accepted")
            fresh = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
            submit_once(fresh, form_info)
            after = conn.execute("SELECT * FROM appointments WHERE id=?", (row_id,)).fetchone()
        if after["status"] == CONFIRMED:
            return self._redirect_flash("/", "ok", f"重新提交成功，预约号 {after['appointment_no']}")
        return self._redirect_flash("/", "warn", "重新提交后回执仍不明确，已再次置为待确认并将自动核对")


def create_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    db()  # 初始化 + 崩溃恢复
    t = threading.Thread(target=_reconciler_loop, daemon=True, name="wh-reconciler")
    t.start()
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = create_server()
    print(f"[warehouse] QC app listening on http://{HOST}:{PORT} (db={DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
