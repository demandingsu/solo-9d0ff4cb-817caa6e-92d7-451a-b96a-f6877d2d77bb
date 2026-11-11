#!/usr/bin/env python3
"""端到端测试：并发防重、回执中断、查询失败保待确认、明确未受理才重试、重启核对。

仅使用标准库；运行：python3 -I tests/test_e2e.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="qc-e2e-")
MFG_PORT = 18081
WH_PORT = 18080
DETAIL_URL = f"http://127.0.0.1:{MFG_PORT}/recall/recall-2026-001"

os.environ["MFG_DB_PATH"] = os.path.join(TMP, "mfg.db")
os.environ["WH_DB_PATH"] = os.path.join(TMP, "wh.db")
os.environ["PORT"] = "0"  # create_server 时显式传端口
os.environ["RECONCILE_INTERVAL"] = "2"
os.environ["RECONCILE_BACKOFF"] = "1"
sys.path.insert(0, os.path.join(ROOT, "warehouse"))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mfg = _load("mfg_server", os.path.join(ROOT, "manufacturer", "server.py"))
wh = _load("wh_server", os.path.join(ROOT, "warehouse", "server.py"))


# ----------------------------------------------------------------- HTTP 工具
def _req(method: str, url: str, data: dict | None = None, follow=False):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=15)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def get(url, follow=True):
    return _req("GET", url, follow=follow)


def post(url, data):
    return _req("POST", url, data)


def wh_create(handover_no: str, detail_url: str = DETAIL_URL, boxes: int = 5, day="2026-11-15"):
    return post(
        f"http://127.0.0.1:{WH_PORT}/appointments",
        {"detail_url": detail_url, "handover_no": handover_no, "box_count": str(boxes), "pickup_date": day},
    )


def wh_list_rows():
    conn = wh.db()
    return conn.execute("SELECT * FROM appointments ORDER BY id").fetchall()


def row_by_handover(handover_no: str):
    return wh.db().execute("SELECT * FROM appointments WHERE handover_no=?", (handover_no,)).fetchone()


def mfg_set_scenario(mode: str, duration: int = 0):
    post(f"http://127.0.0.1:{MFG_PORT}/admin/scenario", {"mode": mode, "duration": str(duration)})


def mfg_lookup(handover_no: str):
    status, body, _ = get(f"http://127.0.0.1:{MFG_PORT}/api/appointments/lookup?handover_no={handover_no}")
    return status, json.loads(body)


def mfg_metrics():
    _, body, _ = get(f"http://127.0.0.1:{MFG_PORT}/admin/metrics")
    return json.loads(body)


def wait_until(desc, predicate, timeout=20):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return
        time.sleep(0.3)
    raise AssertionError(f"等待超时：{desc}（最后值={last!r}）")


# ----------------------------------------------------------------- 启动服务
mfg_server: ThreadingHTTPServer = mfg.create_server("127.0.0.1", MFG_PORT)
wh_server: ThreadingHTTPServer = wh.create_server("127.0.0.1", WH_PORT)
threading.Thread(target=mfg_server.serve_forever, daemon=True).start()
threading.Thread(target=wh_server.serve_forever, daemon=True).start()
wait_until("两个服务健康", lambda: get(f"http://127.0.0.1:{WH_PORT}/healthz")[0] == 200
           and get(f"http://127.0.0.1:{MFG_PORT}/healthz")[0] == 200)

PASS = 0


def check(name: str, cond, extra=""):
    global PASS
    assert cond, f"{name} 失败 {extra}"
    PASS += 1
    print(f"  ✓ {name}")


# ----------------------------------------------------------------- 测试用例
def test_01_pages_and_form():
    print("[1] 详情页表单发现与工作台页面")
    status, body, _ = get(DETAIL_URL)
    text = body.decode()
    check("详情页 200", status == 200)
    check("含预约表单 action", 'action="/api/appointments"' in text)
    check("含 appointment-lookup 声明", 'rel="appointment-lookup"' in text and "/api/appointments/lookup" in text)
    check("含交接单号字段", 'name="handover_no"' in text)
    status, body, _ = get(f"http://127.0.0.1:{WH_PORT}/")
    check("工作台 200 且含录入字段", status == 200 and b'name="handover_no"' in body and b'name="detail_url"' in body)
    info = wh.parse_recall_page(text, DETAIL_URL)
    check("解析出 submit/lookup", info["submit_url"].endswith("/api/appointments")
          and info["lookup_url"].endswith("/api/appointments/lookup"))


def test_02_happy_path():
    print("[2] 正常预约全流程")
    ho = "HO-E2E-02"
    status, _, headers = wh_create(ho)
    check("POST 后 303 跳转", status == 303 and headers.get("Location") == "/")
    row = row_by_handover(ho)
    check("状态 CONFIRMED", row["status"] == wh.CONFIRMED, row["status"])
    check("预约号已保存", row["appointment_no"].startswith("MFG-"), row["appointment_no"])
    check("公告来源已保存", row["source_title"] and row["detail_url"] == DETAIL_URL)
    _, data = mfg_lookup(ho)
    check("生产商侧可查同预约号", data["accepted"] and data["appointment_no"] == row["appointment_no"])
    status, body, _ = get(f"http://127.0.0.1:{WH_PORT}/appointments/{row['id']}")
    check("详情页展示状态/预约号/来源", status == 200 and row["appointment_no"].encode() in body
          and "已确认".encode() in body and DETAIL_URL.encode() in body)


def test_03_duplicate_handover_blocked():
    print("[3] 同交接单号重复下单被拦截")
    ho = "HO-E2E-03"
    wh_create(ho)
    before = mfg_metrics()["appointment_posts"]
    status, _, headers = wh_create(ho)
    after = mfg_metrics()["appointment_posts"]
    check("重复请求 303", status == 303)
    check("生产商未收到第二次提交", after == before, f"{before}->{after}")
    check("flash 提示已存在", "已存在" in urllib.parse.unquote(headers.get("Set-Cookie", "")))
    check("只有一条本地记录", len([r for r in wh_list_rows() if r["handover_no"] == ho]) == 1)


def test_04_receipt_interrupt_then_lookup_confirms():
    print("[4] 已受理但回执中断 -> 自动核对出原预约号，不重复提交")
    ho = "HO-E2E-04"
    mfg_set_scenario("receipt_interrupt_once")
    before = mfg_metrics()["appointment_posts"]
    wh_create(ho)
    row = row_by_handover(ho)
    check("回执中断后置待确认", row["status"] == wh.PENDING, row["status"])
    check("待确认时无预约号", not row["appointment_no"])
    check("生产商实际只收到 1 次提交", mfg_metrics()["appointment_posts"] == before + 1)
    _, data = mfg_lookup(ho)
    original_no = data["appointment_no"]
    check("生产商侧已受理", data["accepted"] and original_no)
    # 后台核对器自动恢复
    wait_until("自动核对为 CONFIRMED", lambda: row_by_handover(ho)["status"] == wh.CONFIRMED)
    row = row_by_handover(ho)
    check("展示的是原预约号", row["appointment_no"] == original_no)
    check("生产商提交次数未增加", mfg_metrics()["appointment_posts"] == before + 1)


def test_05_manual_reconcile_and_connection_drop():
    print("[5] 断连式回执中断 + 手动立即核对")
    ho = "HO-E2E-05"
    mfg.scenario.interrupt_style = "connection_drop"
    mfg_set_scenario("receipt_interrupt_once")
    wh_create(ho)
    row = row_by_handover(ho)
    check("断连后同样置待确认", row["status"] == wh.PENDING, row["note"])
    mfg.scenario.interrupt_style = "empty_502"
    status, _, headers = post(f"http://127.0.0.1:{WH_PORT}/appointments/{row['id']}/reconcile", {})
    check("手动核对 303", status == 303)
    row = row_by_handover(ho)
    check("手动核对拿到原预约号", row["status"] == wh.CONFIRMED and row["appointment_no"].startswith("MFG-"))
    check("无重复提交", len([r for r in wh_list_rows() if r["handover_no"] == ho]) == 1)


def test_06_query_failure_keeps_pending():
    print("[6] 查询失败：保持待确认且不再提交")
    ho = "HO-E2E-06"
    mfg_set_scenario("receipt_interrupt_once")
    posts_before = mfg_metrics()["appointment_posts"]
    wh_create(ho)
    check("先进入待确认", row_by_handover(ho)["status"] == wh.PENDING)
    mfg_set_scenario("query_failure_timed", 30)
    time.sleep(3.5)  # 等后台核对器跑至少一轮
    row = row_by_handover(ho)
    check("查询失败仍保持待确认", row["status"] == wh.PENDING, row["status"])
    check("没有任何新提交", mfg_metrics()["appointment_posts"] == posts_before + 1)
    # 手动核对也必须失败且不改状态、不提交
    status, _, _ = post(f"http://127.0.0.1:{WH_PORT}/appointments/{row['id']}/reconcile", {})
    check("手动核对返回 303", status == 303)
    check("手动核对后仍待确认", row_by_handover(ho)["status"] == wh.PENDING)
    check("提交次数依旧不变", mfg_metrics()["appointment_posts"] == posts_before + 1)
    # 恢复查询后自动核对成功
    mfg_set_scenario("normal")
    wait_until("查询恢复后确认", lambda: row_by_handover(ho)["status"] == wh.CONFIRMED)


def test_07_outage_then_not_accepted_then_retry():
    print("[7] 明确未受理才可重试（预约接口故障不落库场景）")
    ho = "HO-E2E-07"
    mfg_set_scenario("booking_outage_timed", 30)
    posts_before = mfg_metrics()["appointment_posts"]
    wh_create(ho)
    row = row_by_handover(ho)
    check("故障响应进入待确认", row["status"] == wh.PENDING)
    mfg_set_scenario("normal")
    wait_until("核对为明确未受理", lambda: row_by_handover(ho)["status"] == wh.NOT_ACCEPTED)
    _, data = mfg_lookup(ho)
    check("生产商确实无此单", data["accepted"] is False)
    # 待确认状态下禁止重试
    row = row_by_handover(ho)
    status, _, _ = post(f"http://127.0.0.1:{WH_PORT}/appointments/{row['id']}/retry", {})
    # 此时已是 NOT_ACCEPTED，重试应成功
    check("重试 303", status == 303)
    wait_until("重试后确认", lambda: row_by_handover(ho)["status"] == wh.CONFIRMED)
    row = row_by_handover(ho)
    check("重试拿到预约号", row["appointment_no"].startswith("MFG-"))
    # 总提交次数 = 失败 1 次 + 成功 1 次
    check("生产商共收到 2 次请求（故障1+重试1）", mfg_metrics()["appointment_posts"] == posts_before + 2)


def test_08_concurrent_same_handover():
    print("[8] 同号并发点击：只提交一次")
    ho = "HO-E2E-08"
    posts_before = mfg_metrics()["appointment_posts"]
    errors: list[Exception] = []
    results = []

    def worker():
        try:
            results.append(wh_create(ho))
        except Exception as exc:  # 并发下任何异常都算失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发请求无异常", not errors, str(errors))
    check("10 个请求都有响应", len(results) == 10)
    check("本地仅一条记录", len([r for r in wh_list_rows() if r["handover_no"] == ho]) == 1)
    row = row_by_handover(ho)
    check("记录已确认", row["status"] == wh.CONFIRMED)
    check("生产商只受理一次（仅 1 次 POST）", mfg_metrics()["appointment_posts"] == posts_before + 1,
          str(mfg_metrics()))
    _, data = mfg_lookup(ho)
    check("生产商侧仅一条且预约号一致", data["accepted"] and data["appointment_no"] == row["appointment_no"])


def test_09_restart_reconciles_pending():
    global wh_server
    print("[9] 应用重启后自动核对残留待确认/提交中交接单")
    ho = "HO-E2E-09"
    mfg_set_scenario("receipt_interrupt_once")
    wh_create(ho)
    check("构造出待确认", row_by_handover(ho)["status"] == wh.PENDING)
    original_no = mfg_lookup(ho)[1]["appointment_no"]
    posts_before_restart = mfg_metrics()["appointment_posts"]

    # 停止旧服务（含后台核对线程），用同一数据库重新拉起 -> 模拟进程重启
    wh_server.shutdown()
    wh_server.server_close()
    time.sleep(0.5)
    new_server = wh.create_server("127.0.0.1", WH_PORT)
    threading.Thread(target=new_server.serve_forever, daemon=True).start()
    try:
        wait_until("新实例健康", lambda: get(f"http://127.0.0.1:{WH_PORT}/healthz")[0] == 200)
        wait_until("重启后自动核对确认", lambda: row_by_handover(ho)["status"] == wh.CONFIRMED)
        row = row_by_handover(ho)
        check("恢复出的是原预约号", row["appointment_no"] == original_no)
        check("重启未产生重复提交", mfg_metrics()["appointment_posts"] == posts_before_restart)
        mfg_rows = mfg.db().execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE handover_no=?", (ho,)
        ).fetchone()
        check("生产商库同号仅 1 条", mfg_rows["c"] == 1)
    finally:
        new_server.shutdown()
        new_server.server_close()
        time.sleep(0.3)
        # 重新拉起供后续测试
        restarted = wh.create_server("127.0.0.1", WH_PORT)
        threading.Thread(target=restarted.serve_forever, daemon=True).start()
        wait_until("服务恢复", lambda: get(f"http://127.0.0.1:{WH_PORT}/healthz")[0] == 200)
        wh_server = restarted


def test_10_unreachable_submit_then_reconcile():
    print("[10] 提交传输失败（结果不明）-> 待确认，绝不盲重试")
    ho = "HO-E2E-10"
    # 直接种入一条待确认记录：提交地址指向不存在的端口
    conn = wh.db()
    now = wh._now()
    conn.execute(
        """INSERT INTO appointments
           (handover_no, detail_url, box_count, pickup_date, status, submit_url, lookup_url,
            announcement_id, source_title, note, created_at, updated_at, last_reconcile_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ho, DETAIL_URL, 3, "2026-11-16", wh.PENDING,
         "http://127.0.0.1:19999/api/appointments",
         f"http://127.0.0.1:{MFG_PORT}/api/appointments/lookup",
         "recall-2026-001", "种子数据", "提交地址不可达", now, now, 0.0),
    )
    conn.commit()
    posts_before = mfg_metrics()["appointment_posts"]
    time.sleep(3.5)  # 后台核对走 lookup（可达），生产商无此单 -> NOT_ACCEPTED
    row = row_by_handover(ho)
    check("经查询明确未受理", row["status"] == wh.NOT_ACCEPTED, row["status"])
    check("全程没有向坏地址之外重复提交（生产商 POST 不增加）",
          mfg_metrics()["appointment_posts"] == posts_before)
    # 修正提交地址后重试成功
    conn.execute("UPDATE appointments SET submit_url=? WHERE handover_no=?",
                 (f"http://127.0.0.1:{MFG_PORT}/api/appointments", ho))
    conn.commit()
    row = row_by_handover(ho)
    post(f"http://127.0.0.1:{WH_PORT}/appointments/{row['id']}/retry", {})
    wait_until("重试成功", lambda: row_by_handover(ho)["status"] == wh.CONFIRMED)
    check("重试后有预约号", row_by_handover(ho)["appointment_no"].startswith("MFG-"))


def test_11_validation_and_logs():
    print("[11] 输入校验与事件日志")
    status, _, _ = post(f"http://127.0.0.1:{WH_PORT}/appointments",
                        {"detail_url": "ftp://x", "handover_no": "HO-BAD", "box_count": "1", "pickup_date": "2026-11-15"})
    check("非法 URL 被拒（303+flash）", status == 303)
    status, _, _ = post(f"http://127.0.0.1:{WH_PORT}/appointments",
                        {"detail_url": DETAIL_URL, "handover_no": "HO-BAD2", "box_count": "0", "pickup_date": "2026-11-15"})
    check("箱数 0 被拒", status == 303)
    check("非法单未入库", row_by_handover("HO-BAD") is None and row_by_handover("HO-BAD2") is None)
    logs = wh.db().execute("SELECT COUNT(*) AS c FROM event_log").fetchone()["c"]
    check("事件日志有记录", logs >= 5)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failures += 1
            print(f"  ✗ {t.__name__}: {exc!r}")
    print(f"\n通过 {PASS} 项断言，失败 {failures} 个用例；临时目录 {TMP}")
    mfg_server.shutdown()
    sys.exit(1 if failures else 0)
