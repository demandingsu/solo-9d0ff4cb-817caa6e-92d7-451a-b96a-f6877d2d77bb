#!/usr/bin/env python3
"""出站 HTTP 客户端（urllib 标准库实现，区分传输中断与业务响应）。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit

DEFAULT_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "8"))


class HttpTransportError(Exception):
    """连接被拒、断连、超时等传输层问题。"""


def apply_url_map(url: str) -> str:
    """容器内地址改写：浏览器用 localhost，容器内走 compose 网络名。"""
    src = os.environ.get("URL_MAP_FROM", "")
    dst = os.environ.get("URL_MAP_TO", "")
    if src and url.startswith(src):
        return dst + url[len(src):]
    return url


def _request(url: str, data: bytes | None, headers: dict[str, str], method: str):
    req = urllib.request.Request(apply_url_map(url), data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise HttpTransportError(str(exc)) from exc


def http_get(url: str) -> tuple[int, bytes]:
    return _request(url, None, {"Accept": "text/html,application/json"}, "GET")


def http_post_form(url: str, fields: dict[str, str]) -> tuple[int, bytes]:
    body = urlencode(fields).encode("utf-8")
    return _request(
        url,
        body,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        "POST",
    )


def parse_json(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
