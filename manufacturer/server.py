#!/usr/bin/env python3
"""生产商召回预约示例站（纯标准库，离线可运行）。

能力：
  GET  /                         公告列表 + 故障场景控制台
  GET  /recall/<announcement_id> 召回详情页（内含预约表单与受理查询入口声明）
  POST /api/appointments         预约受理接口，按交接单号幂等（同号至多受理一次）
  GET  /api/appointments/lookup  按交接单号查询受理结果
  GET  /admin/appointments       已受理预约一览
  GET  /api/scenario             故障场景状态（供页面轮询）
  POST /admin/scenario           切换故障场景
  GET  /admin/metrics            计数器（测试/核查用）
  POST /admin/metrics/reset      重置计数器
  GET  /healthz

故障场景：
  normal                    正常
  receipt_interrupt_once    下一次 *新单* 受理后回执中断（502 空响应/断连），随后自动恢复
  receipt_interrupt_timed   时间窗内新单受理后回执中断
  query_failure_timed       时间窗内受理查询接口故障（503）
  booking_outage_timed      时间窗内预约接口故障且不落库（503），用于制造“明确未受理”
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("MFG_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "manufacturer.db"))
PORT = int(os.environ.get("PORT", "8081"))
HOST = os.environ.get("HOST", "0.0.0.0")

RECALLS = {
    "recall-2026-001": {
        "title": "关于召回 A 批次冷藏面点产品的公告",
        "product": "XX 牌鲜肉包（A 批次，生产日期 2026-10-20）",
        "published": "2026-11-05",
        "reason": "部分产品外包材密封不严，存在冷链污染风险，依据食品安全抽检结论实施召回。",
        "contact": "生产商质量部 400-000-1001",
    },
    "recall-2026-002": {
        "title": "关于召回 B 批次常温饮品的公告",
        "product": "YY 牌豆奶（B 批次，生产日期 2026-10-28）",
        "published": "2026-11-08",
        "reason": "灌装工序温控偏差，个别产品存在胀包隐患，现主动召回并预约上门取件。",
        "contact": "生产商客服中心 400-000-1002",
    },
}

_db_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_no TEXT NOT NULL UNIQUE,
                handover_no TEXT NOT NULL UNIQUE,
                announcement_id TEXT NOT NULL,
                box_count INTEGER NOT NULL,
                pickup_date TEXT NOT NULL,
                contact TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ACCEPTED',
                created_at TEXT NOT NULL
            );
            """
        )
        _conn.commit()
    return _conn


class ScenarioState:
    """故障场景开关（进程内状态，带自动到期恢复）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "normal"
        self.expire_at = 0.0
        self.interrupt_style = os.environ.get("MFG_INTERRUPT_STYLE", "empty_502")  # empty_502 | connection_drop

    def set(self, mode: str, duration: int = 0) -> None:
        with self.lock:
            self.mode = mode
            self.expire_at = time.time() + duration if duration > 0 else 0.0

    def effective(self) -> str:
        """返回当前生效模式，过期自动恢复 normal。"""
        with self.lock:
            if self.mode != "normal" and self.expire_at and time.time() >= self.expire_at:
                self.mode = "normal"
                self.expire_at = 0.0
            return self.mode

    def remaining(self) -> int:
        with self.lock:
            if self.expire_at:
                return max(0, int(round(self.expire_at - time.time())))
            return 0

    def consume_once(self) -> None:
        with self.lock:
            if self.mode == "receipt_interrupt_once":
                self.mode = "normal"
                self.expire_at = 0.0


scenario = ScenarioState()


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.appointment_posts = 0
        self.lookup_requests = 0

    def inc_post(self) -> None:
        with self.lock:
            self.appointment_posts += 1

    def inc_lookup(self) -> None:
        with self.lock:
            self.lookup_requests += 1

    def reset(self) -> None:
        with self.lock:
            self.appointment_posts = 0
            self.lookup_requests = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {"appointment_posts": self.appointment_posts, "lookup_requests": self.lookup_requests}


metrics = Metrics()


class _InterruptResponse(Exception):
    """已受理但需要直接掐断连接的内部信号。"""


def _next_appointment_no() -> str:
    today = date.today().strftime("%Y%m%d")
    prefix = f"MFG-{today}-"
    with _db_lock:
        row = db().execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE appointment_no LIKE ?", (prefix + "%",)
        ).fetchone()
        return f"{prefix}{row['c'] + 1:04d}"


def _insert_appointment(fields: dict[str, str]) -> tuple[sqlite3.Row, bool]:
    """返回 (预约行, 是否新建)。同交接单号幂等返回既有记录。"""
    handover_no = fields["handover_no"].strip()
    with _db_lock:
        existing = db().execute(
            "SELECT * FROM appointments WHERE handover_no = ?", (handover_no,)
        ).fetchone()
        if existing:
            return existing, False
        appointment_no = _next_appointment_no()
        db().execute(
            """INSERT INTO appointments
               (appointment_no, handover_no, announcement_id, box_count, pickup_date, contact, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'ACCEPTED', ?)""",
            (
                appointment_no,
                handover_no,
                fields.get("announcement_id", ""),
                int(fields["box_count"]),
                fields["pickup_date"],
                fields.get("contact", ""),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db().commit()
        row = db().execute("SELECT * FROM appointments WHERE handover_no = ?", (handover_no,)).fetchone()
        return row, True


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


PAGE_CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f3a5f;color:#fff;padding:18px 28px}
main{max-width:960px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{margin-top:0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #dde1e7;padding:8px 10px;text-align:left}
th{background:#f0f3f7}
form label{display:block;margin:10px 0;font-size:14px}
input{padding:8px;border:1px solid #bbb;border-radius:6px;min-width:240px}
button,.btn{appearance:none;border:0;border-radius:6px;padding:9px 16px;background:#1f6feb;color:#fff;cursor:pointer;text-decoration:none;display:inline-block;margin:4px 6px 4px 0;font-size:14px}
button.secondary{background:#57606a}
button.warn{background:#bf570a}
button.danger{background:#b42318}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.badge.normal{background:#dafbe1;color:#1a7f37}
.badge.fault{background:#fff1e5;color:#bc4000}
.muted{color:#666;font-size:13px}
code{background:#eef1f4;padding:2px 6px;border-radius:4px}
"""


def _layout(title: str, body: str) -> bytes:
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - 生产商召回预约平台（本地示例）</title>
<style>{PAGE_CSS}</style></head>
<body>
<header><h1 style="margin:0;font-size:20px">生产商召回预约平台 <span class="muted" style="color:#bcd">本地示例站</span></h1></header>
<main>{body}</main>
</body></html>"""
    return html.encode("utf-8")


def home_page() -> bytes:
    mode = scenario.effective()
    remaining = scenario.remaining()
    badge_cls = "normal" if mode == "normal" else "fault"
    cards = ""
    for rid, info in RECALLS.items():
        cards += f"""
        <div class="card">
          <h2><a href="/recall/{rid}">{info['title']}</a></h2>
          <p class="muted">公告编号：<code>{rid}</code> ｜ 发布日期：{info['published']}</p>
          <p>涉及产品：{info['product']}</p>
          <p>召回原因：{info['reason']}</p>
          <a class="btn" href="/recall/{rid}">查看详情并预约取件</a>
        </div>"""
    body = f"""
    <div class="card">
      <h2>故障场景控制台</h2>
      <p>当前场景：<span id="mode-badge" class="badge {badge_cls}">{mode}</span>
         <span id="countdown" class="muted">{('剩余 ' + str(remaining) + ' 秒') if remaining else ''}</span></p>
      <form method="post" action="/admin/scenario">
        <button name="mode" value="normal" class="secondary">恢复正常</button>
        <button name="mode" value="receipt_interrupt_once" class="warn">模拟：受理成功但回执中断（仅下一次新单）</button>
        <button name="mode" value="receipt_interrupt_timed" class="warn">模拟：回执中断（60 秒窗口）</button>
        <button name="mode" value="query_failure_timed" class="danger">模拟：受理查询故障（60 秒窗口）</button>
        <button name="mode" value="booking_outage_timed" class="danger">模拟：预约接口故障且不落库（60 秒窗口）</button>
      </form>
      <p class="muted">
        “回执中断”指预约已写入受理库，但 HTTP 响应以 502 空响应（或直接断连）返回，调用方无法从回执得知预约号；
        调用方应改用 <code>/api/appointments/lookup</code> 按交接单号核对，不得盲目重复提交。
      </p>
      <p><a class="btn secondary" href="/admin/appointments">查看已受理预约</a>
         <a class="btn secondary" href="/admin/metrics">查看接口计数</a></p>
    </div>
    {cards}
    <script>
    async function poll(){{
      try{{
        const r = await fetch('/api/scenario'); const s = await r.json();
        const b = document.getElementById('mode-badge');
        b.textContent = s.mode + (s.remaining>0 ? '（'+s.remaining+'s）' : '');
        b.className = 'badge ' + (s.mode==='normal' ? 'normal' : 'fault');
      }}catch(e){{}}
    }}
    setInterval(poll, 1000);
    </script>"""
    return _layout("首页", body)


def recall_page(rid: str) -> bytes | None:
    info = RECALLS.get(rid)
    if not info:
        return None
    body = f"""
    <div class="card">
      <h2>{info['title']}</h2>
      <p class="muted">公告编号：<code>{rid}</code> ｜ 发布日期：{info['published']} ｜ {info['contact']}</p>
      <p><strong>涉及产品：</strong>{info['product']}</p>
      <p><strong>召回原因：</strong>{info['reason']}</p>
      <p><strong>处置方式：</strong>请持有隔离货物交接单的仓库质控人员通过下表预约上门取件，本站按交接单号至多受理一次。</p>
    </div>
    <div class="card">
      <h2>取件预约表单</h2>
      <form id="appointment-form" method="post" action="/api/appointments"
            data-lookup="/api/appointments/lookup">
        <input type="hidden" name="announcement_id" value="{rid}">
        <label>交接单号 <input name="handover_no" required maxlength="40" placeholder="例如 HO-20261111-0007"></label>
        <label>箱数 <input name="box_count" type="number" min="1" max="99999" required></label>
        <label>取件日期 <input name="pickup_date" type="date" required></label>
        <label>联系人/电话 <input name="contact" maxlength="60" placeholder="选填"></label>
        <button type="submit">提交预约</button>
      </form>
      <p class="muted">受理结果查询入口（供回执中断后核对）：
        <link-rel style="display:none"></link-rel>
        <code>GET /api/appointments/lookup?handover_no=交接单号</code>
        （同时以 <code>link[rel=appointment-lookup]</code> 与表单 <code>data-lookup</code> 声明）
      </p>
    </div>
    <p><a class="btn secondary" href="/">← 返回公告列表</a></p>"""
    # link 标签放在 head 更规范，这里额外在 head 注入
    html = _layout(info["title"], body).decode("utf-8")
    html = html.replace(
        "<style>",
        '<link rel="appointment-lookup" href="/api/appointments/lookup">\n<style>',
        1,
    )
    return html.encode("utf-8")


def admin_appointments_page() -> bytes:
    with _db_lock:
        rows = db().execute("SELECT * FROM appointments ORDER BY id DESC").fetchall()
    trs = "".join(
        f"<tr><td>{r['appointment_no']}</td><td>{r['handover_no']}</td><td>{r['announcement_id']}</td>"
        f"<td>{r['box_count']}</td><td>{r['pickup_date']}</td><td>{r['contact']}</td>"
        f"<td>{r['status']}</td><td>{r['created_at']}</td></tr>"
        for r in rows
    )
    body = f"""
    <div class="card">
      <h2>已受理预约（共 {len(rows)} 条）</h2>
      <table><thead><tr>
        <th>预约号</th><th>交接单号</th><th>公告编号</th><th>箱数</th><th>取件日期</th>
        <th>联系人</th><th>状态</th><th>受理时间</th>
      </tr></thead><tbody>{trs or '<tr><td colspan="8" class="muted">暂无记录</td></tr>'}</tbody></table>
      <p style="margin-top:16px"><a class="btn secondary" href="/">← 返回首页</a></p>
    </div>"""
    return _layout("已受理预约", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "MfgExample/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[mfg] {self.address_string()} {fmt % args}")

    # ---------- 基础工具 ----------
    def _send_json(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _broken_receipt(self) -> None:
        """模拟受理成功但回执中断。"""
        if scenario.interrupt_style == "connection_drop":
            self.close_connection = True
            try:
                self.connection.shutdown(2)  # SHUT_RDWR
            except OSError:
                pass
            self.connection.close()
            raise _InterruptResponse()
        # 默认：502 + 空响应体 + 关闭连接
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                return self._send_html(200, home_page())
            if path == "/healthz":
                return self._send_json(200, {"ok": True})
            if path.startswith("/recall/"):
                page = recall_page(path.rsplit("/", 1)[-1])
                return self._send_html(200 if page else 404, page or b"announcement not found")
            if path == "/admin/appointments":
                return self._send_html(200, admin_appointments_page())
            if path == "/api/scenario":
                return self._send_json(
                    200, {"mode": scenario.effective(), "remaining": scenario.remaining()}
                )
            if path == "/admin/metrics":
                return self._send_json(200, metrics.snapshot())
            if path == "/api/appointments/lookup":
                return self._handle_lookup(qs)
            return self._send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/appointments":
                return self._handle_create()
            if path == "/admin/scenario":
                return self._handle_set_scenario()
            if path == "/admin/metrics/reset":
                metrics.reset()
                return self._send_json(200, {"ok": True})
            return self._send_json(404, {"error": "not found"})
        except _InterruptResponse:
            return
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---------- 业务处理 ----------
    def _handle_create(self) -> None:
        metrics.inc_post()
        mode = scenario.effective()
        if mode == "booking_outage_timed":
            return self._send_json(503, {"error": "booking service temporarily unavailable (not stored)"})

        fields = self._read_form()
        try:
            if not fields.get("handover_no", "").strip():
                raise ValueError("handover_no is required")
            box = int(fields.get("box_count", ""))
            if box <= 0:
                raise ValueError("box_count must be positive")
            pickup = fields.get("pickup_date", "")
            datetime.strptime(pickup, "%Y-%m-%d")  # 校验日期格式
        except (ValueError, TypeError) as exc:
            return self._send_json(400, {"error": f"invalid payload: {exc}"})

        row, created = _insert_appointment(fields)
        data = _row_to_dict(row)
        data["accepted"] = True
        data["duplicate"] = not created

        # 仅“新单”且处于回执中断场景时，吞掉回执
        if created and mode in ("receipt_interrupt_once", "receipt_interrupt_timed"):
            if mode == "receipt_interrupt_once":
                scenario.consume_once()
            print(f"[mfg] appointment {row['appointment_no']} accepted but receipt interrupted ({mode})")
            return self._broken_receipt()
        return self._send_json(200, data)

    def _handle_lookup(self, qs: dict[str, list[str]]) -> None:
        metrics.inc_lookup()
        if scenario.effective() == "query_failure_timed":
            return self._send_json(503, {"error": "lookup service temporarily unavailable"})
        handover_no = (qs.get("handover_no") or [""])[0].strip()
        if not handover_no:
            return self._send_json(400, {"error": "handover_no is required"})
        with _db_lock:
            row = db().execute(
                "SELECT * FROM appointments WHERE handover_no = ?", (handover_no,)
            ).fetchone()
        if row is None:
            # 明确未受理：HTTP 200 + accepted=false，与“查询失败”严格区分
            return self._send_json(200, {"accepted": False, "handover_no": handover_no})
        data = _row_to_dict(row)
        data["accepted"] = True
        return self._send_json(200, data)

    def _handle_set_scenario(self) -> None:
        form = self._read_form()
        mode = form.get("mode", "normal")
        duration = int(form.get("duration", "0") or "0")
        timed_modes = {"receipt_interrupt_timed", "query_failure_timed", "booking_outage_timed"}
        if mode in timed_modes and duration <= 0:
            duration = 60
        if mode not in {
            "normal",
            "receipt_interrupt_once",
            "receipt_interrupt_timed",
            "query_failure_timed",
            "booking_outage_timed",
        }:
            mode = "normal"
        scenario.set(mode, duration)
        # 浏览器操作走 303 回首页
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _scenario_watchdog() -> None:
    while True:
        time.sleep(1)
        scenario.effective()  # 触发到期自动恢复


def create_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    db()  # 初始化
    t = threading.Thread(target=_scenario_watchdog, daemon=True, name="mfg-scenario-watchdog")
    t.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


if __name__ == "__main__":
    server = create_server()
    print(f"[mfg] manufacturer example site listening on http://{HOST}:{PORT} (db={DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
