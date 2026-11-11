#!/usr/bin/env python3
"""解析生产商召回详情页中的预约表单与受理查询入口。"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin


class _FormExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.lookup_url: str | None = None
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)
        if tag.lower() == "link" and attrs.get("rel", "").lower() == "appointment-lookup":
            self.lookup_url = attrs.get("href")
        elif tag.lower() == "form":
            self._current = {
                "action": attrs.get("action", ""),
                "method": attrs.get("method", "post").lower(),
                "fields": [],
                "lookup": attrs.get("data-lookup"),
            }
        elif tag.lower() in ("input", "select", "textarea") and self._current is not None:
            name = attrs.get("name")
            if name:
                self._current["fields"].append(
                    {"name": name, "value": attrs.get("value", ""), "type": attrs.get("type", "text")}
                )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


class RecallPageError(Exception):
    pass


def parse_recall_page(html: str, detail_url: str) -> dict:
    """从详情页 HTML 提取预约表单提交地址、字段与受理查询入口。"""
    parser = _FormExtractor()
    parser.feed(html)

    def _is_appointment_form(form: dict) -> bool:
        names = {f["name"] for f in form["fields"]}
        return "handover_no" in names

    target = next((f for f in parser.forms if _is_appointment_form(f)), None)
    if target is None:
        raise RecallPageError("详情页中未找到包含交接单号字段的预约表单")

    lookup = target.get("lookup") or parser.lookup_url
    if not lookup:
        raise RecallPageError("详情页未声明受理结果查询入口（appointment-lookup）")

    return {
        "submit_url": urljoin(detail_url, target["action"] or detail_url),
        "method": target["method"],
        "fields": target["fields"],
        "lookup_url": urljoin(detail_url, lookup),
    }


def build_submission_payload(form_fields: list[dict], values: dict[str, str]) -> dict[str, str]:
    """以表单原有字段为骨架填入本次预约值，避免漏提隐藏字段。"""
    payload: dict[str, str] = {}
    for field in form_fields:
        name = field["name"]
        if name in values:
            payload[name] = values[name]
        elif field["type"] != "submit":
            payload[name] = field["value"]
    # 兜底：确保四个核心字段都在
    for key in ("handover_no", "box_count", "pickup_date"):
        payload.setdefault(key, values[key])
    return payload
