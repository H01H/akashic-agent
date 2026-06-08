from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ACK_FILE = BASE_DIR / "ack_state.json"
CACHE_FILE = BASE_DIR / "cache_events.json"
FEEDS_FILE = BASE_DIR / "feeds.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stable_id(*parts: str) -> str:
    raw = "|".join(str(p or "").strip() for p in parts)
    return "agent_news_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_acks() -> dict[str, float]:
    raw = _load_json(ACK_FILE, {})
    return raw if isinstance(raw, dict) else {}


def _save_acks(acks: dict[str, float]) -> None:
    _save_json(ACK_FILE, acks)


def _is_acked(event_id: str) -> bool:
    until = float(_load_acks().get(event_id, 0) or 0)
    return until == float("inf") or time.time() < until


def _load_feeds() -> list[dict[str, str]]:
    raw = _load_json(FEEDS_FILE, [])
    feeds = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("url"):
                feeds.append({
                    "name": str(item.get("name") or "feed"),
                    "url": str(item["url"]),
                })
    return feeds


def _fetch_url(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "akashic-agent-news-mcp/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _normalize_time(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return _now_iso()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except Exception:
        pass
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc).isoformat()
    except Exception:
        return _now_iso()


def _strip_html(text: str) -> str:
    raw = str(text or "")
    out = []
    in_tag = False
    for ch in raw:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return " ".join("".join(out).split())[:800]


def _child_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _parse_feed(xml_text: str, feed_name: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    events: list[dict[str, Any]] = []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    atom_entries = root.findall(".//atom:entry", ns)
    if atom_entries:
        for entry in atom_entries[:10]:
            title = _child_text(entry, ["atom:title"])
            content = _child_text(entry, ["atom:summary", "atom:content"])
            published = _child_text(entry, ["atom:published", "atom:updated"])
            url = ""
            link = entry.find("atom:link", ns)
            if link is not None:
                url = str(link.attrib.get("href", "")).strip()

            event_id = _stable_id(feed_name, title, url, published)
            events.append({
                "kind": "content",
                "event_id": event_id,
                "source_type": "rss",
                "source_name": feed_name,
                "title": title or "(untitled)",
                "content": _strip_html(content or title),
                "url": url,
                "published_at": _normalize_time(published),
            })
        return events

    rss_items = root.findall(".//item")
    for item in rss_items[:10]:
        title = _child_text(item, ["title"])
        content = _child_text(item, ["description", "summary"])
        url = _child_text(item, ["link"])
        published = _child_text(item, ["pubDate", "published", "updated"])

        event_id = _stable_id(feed_name, title, url, published)
        events.append({
            "kind": "content",
            "event_id": event_id,
            "source_type": "rss",
            "source_name": feed_name,
            "title": title or "(untitled)",
            "content": _strip_html(content or title),
            "url": url,
            "published_at": _normalize_time(published),
        })

    return events


def poll_feeds() -> str:
    feeds = _load_feeds()
    events: list[dict[str, Any]] = []

    for feed in feeds:
        try:
            xml_text = _fetch_url(feed["url"])
            events.extend(_parse_feed(xml_text, feed["name"]))
        except Exception as exc:
            events.append({
                "kind": "content",
                "event_id": _stable_id("fetch_error", feed["name"], str(exc)),
                "source_type": "rss",
                "source_name": "agent_news",
                "title": f"{feed['name']} 抓取失败",
                "content": f"RSS/Atom 抓取失败：{exc}",
                "url": feed["url"],
                "published_at": _now_iso(),
            })

    # 去重，保留靠前的新条目
    seen = set()
    deduped = []
    for event in events:
        eid = event.get("event_id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        deduped.append(event)

    _save_json(CACHE_FILE, deduped[:50])
    return "ok"


def get_proactive_events() -> str:
    events = _load_json(CACHE_FILE, [])
    if not isinstance(events, list):
        events = []

    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("kind") != "content":
            continue
        event_id = str(event.get("event_id", "")).strip()
        if not event_id or _is_acked(event_id):
            continue
        result.append(event)

    return json.dumps(result[:10], ensure_ascii=False)


def acknowledge_events(event_ids: list[str], ttl_hours: int = 168) -> str:
    acks = _load_acks()
    ttl = int(ttl_hours or 0)
    until = time.time() + ttl * 3600 if ttl > 0 else float("inf")

    count = 0
    for event_id in event_ids or []:
        eid = str(event_id).strip()
        if not eid:
            continue
        acks[eid] = until
        count += 1

    _save_acks(acks)
    return json.dumps({"ok": True, "acked": count}, ensure_ascii=False)


TOOLS = {
    "get_proactive_events": {
        "description": "返回未 ACK 的 AI Agent 学习内容事件列表。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: get_proactive_events(),
    },
    "acknowledge_events": {
        "description": "ACK 已处理事件，使其在 ttl_hours 内不再返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_ids": {"type": "array", "items": {"type": "string"}},
                "ttl_hours": {"type": "integer", "default": 168},
            },
            "required": ["event_ids"],
        },
        "handler": lambda args: acknowledge_events(
            event_ids=args.get("event_ids", []),
            ttl_hours=args.get("ttl_hours", 168),
        ),
    },
    "poll_feeds": {
        "description": "抓取 feeds.json 中配置的 RSS/Atom 源并缓存内容事件。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: poll_feeds(),
    },
}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent_news", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                    for name, spec in TOOLS.items()
                ]
            },
        }

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool: {name}"},
            }

        try:
            text = TOOLS[name]["handler"](args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    if req_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = _handle(json.loads(line))
            if response is not None:
                _write(response)
        except Exception as exc:
            _write({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            })


if __name__ == "__main__":
    main()