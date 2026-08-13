# -*- coding: utf-8 -*-
"""Attach to an existing DrissionPage Chrome and observe API packets.

This script is intentionally passive: it does not navigate, click, refresh,
or change the browser window. Start it before interacting with the already
open DP tab, then use the page normally.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from DrissionPage import Chromium


DEFAULT_TARGETS = (
    "cgate.zhaopin.com",
    "searchPositionsCompany",
    "searchrecommend",
    "companyJobList",
)
DEFAULT_TAB_HINT = "zhaopin.com/companydetail/"
SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie", "proxy-authorization"}


def json_value(value: Any) -> Any:
    """Convert DP's mapping-like objects to JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    try:
        return {str(k): json_value(v) for k, v in value.items()}
    except AttributeError:
        return str(value)


def parse_post_data(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return json_value(value)


def safe_attribute(value: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name)
    except Exception:  # noqa: BLE001
        return default


def redacted_headers(headers: Any) -> dict[str, Any] | None:
    headers = json_value(headers)
    if not isinstance(headers, dict):
        return headers
    return {
        str(key): "<redacted>" if str(key).lower() in SENSITIVE_HEADER_NAMES else value
        for key, value in headers.items()
    }


def redacted_url(url: Any) -> Any:
    if not isinstance(url, str):
        return url
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def nested_value(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def response_summary(body: Any) -> dict[str, Any]:
    body = json_value(body)
    candidates = (
        ("data.count", nested_value(body, "data", "count")),
        ("data.totalCount", nested_value(body, "data", "totalCount")),
        ("data.total", nested_value(body, "data", "total")),
        ("count", nested_value(body, "count")),
        ("totalCount", nested_value(body, "totalCount")),
    )
    count_path, count = next(((path, value) for path, value in candidates if value is not None), (None, None))
    data = nested_value(body, "data")
    items = None
    if isinstance(data, dict):
        for key in ("list", "items", "positionList", "records"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    return {
        "count_path": count_path,
        "count": count,
        "list_length": len(items) if isinstance(items, list) else None,
        "status_code": body.get("statusCode") if isinstance(body, dict) else None,
        "code": body.get("code") if isinstance(body, dict) else None,
    }


def packet_record(packet: Any, full_body: bool, include_sensitive: bool = False) -> dict[str, Any]:
    request = safe_attribute(packet, "request")
    response = safe_attribute(packet, "response")
    body = safe_attribute(response, "body") if response else None
    post_data = safe_attribute(request, "postData") if request else None
    return {
        "method": safe_attribute(packet, "method"),
        "url": safe_attribute(packet, "url") if include_sensitive else redacted_url(safe_attribute(packet, "url")),
        "resource_type": safe_attribute(packet, "resourceType"),
        "request": {
            "params": json_value(safe_attribute(request, "params")) if include_sensitive and request else None,
            "headers": (
                json_value(safe_attribute(request, "headers"))
                if include_sensitive else redacted_headers(safe_attribute(request, "headers"))
            ) if request else None,
            "cookies": json_value(safe_attribute(request, "cookies", [])) if include_sensitive and request else [],
            "postData": parse_post_data(post_data) if include_sensitive else None,
        },
        "response": {
            "status": safe_attribute(response, "status") if response else None,
            "status_text": safe_attribute(response, "statusText") if response else None,
            "mime_type": safe_attribute(response, "mimeType") if response else None,
            "headers": (
                json_value(safe_attribute(response, "headers"))
                if include_sensitive else redacted_headers(safe_attribute(response, "headers"))
            ) if response else None,
            "summary": response_summary(body),
            **({"body": json_value(body)} if full_body and include_sensitive else {}),
        },
    }


def select_tab(browser: Chromium, tab_id: str | None, hint: str):
    if tab_id:
        return browser.get_tab(tab_id)
    latest = browser.latest_tab
    if hint and hint not in (latest.url or ""):
        print(f"[warn] latest tab does not match {hint!r}: {latest.url}", file=sys.stderr)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="9222", help="existing DP browser port/address (default: 9222)")
    parser.add_argument("--tab-id", help="attach a specific existing tab id instead of latest_tab")
    parser.add_argument("--target", action="append", dest="targets", help="additional URL substring to capture")
    parser.add_argument("--full-body", action="store_true", help="include response bodies with --include-sensitive")
    parser.add_argument("--include-sensitive", action="store_true", help="include raw URLs, cookies, auth headers, request bodies, and responses")
    parser.add_argument("--all", action="store_true", help="capture all network requests instead of only targets")
    parser.add_argument("--out", type=Path, help="also append records as JSONL")
    args = parser.parse_args()

    address: int | str = int(args.port) if str(args.port).isdigit() else args.port
    browser = Chromium(address)
    tab = select_tab(browser, args.tab_id, DEFAULT_TAB_HINT)
    targets = list(dict.fromkeys((*DEFAULT_TARGETS, *(args.targets or []))))
    listen_target: bool | list[str] = True if args.all else targets
    tab.listen.start(listen_target, is_regex=False, method=True, res_type=True)

    print(f"attached: {args.port} tab={tab.tab_id}")
    print(f"url: {tab.url}")
    print(f"targets: {'all' if args.all else targets}")
    print("listening; interact with the existing DP page, Ctrl+C to stop")
    sys.stdout.flush()

    output = args.out.open("a", encoding="utf-8") if args.out else None
    stopped = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    try:
        while not stopped:
            packet = tab.listen.wait(timeout=1, fit_count=False)
            if not packet:
                continue
            packets = packet if isinstance(packet, list) else [packet]
            for item in packets:
                if not item:
                    continue
                record = packet_record(item, args.full_body, args.include_sensitive)
                print("\n=== packet ===")
                print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
                if output:
                    output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    output.flush()
                sys.stdout.flush()
    finally:
        tab.listen.stop()
        if output:
            output.close()
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
