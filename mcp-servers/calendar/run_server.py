#!/usr/bin/env python3
"""MCP Server: 查询日历。实现 JSON-RPC over stdio 协议。"""

import json
import sys
from datetime import date, timedelta, datetime


# ── 工具实现 ──────────────────────────────────────────

def get_today() -> dict:
    today = date.today()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return {
        "date": today.isoformat(),
        "weekday": weekdays[today.weekday()],
        "year": today.year,
        "month": today.month,
        "day": today.day,
    }




def get_week(year: int, month: int, day: int) -> str:
    target_date = date(year, month, day)
    monday = target_date - timedelta(days=target_date.weekday())
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    result = []
    for i in range(7):
        current_date = monday + timedelta(days=i)
        result.append({
            "date": current_date.isoformat(),
            "weekday": weekdays[i],
            "year": current_date.year,
            "month": current_date.month,
            "day": current_date.day,
        })

    return json.dumps(result, ensure_ascii=False)


def days_until(year: int, month: int, day: int) -> dict:
    target = date(year, month, day)
    today = date.today()
    delta = (target - today).days
    return {
        "target": target.isoformat(),
        "today": today.isoformat(),
        "days_remaining": delta,
        "is_past": delta < 0,
    }


# ── 工具定义 ──────────────────────────────────────────

TOOLS = [
    {
        "name": "get_today",
        "description": "获取今天的日期、星期几",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_week",
        "description": "获取包含指定日期的那一整周（周一到周日）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "年份"},
                "month": {"type": "integer", "description": "月份（1-12）"},
                "day": {"type": "integer", "description": "日期"},
            },
            "required": ["year", "month", "day"],
        },
    },
    {
        "name": "days_until",
        "description": "计算距离指定日期还有多少天",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "年份"},
                "month": {"type": "integer", "description": "月份（1-12）"},
                "day": {"type": "integer", "description": "日期"},
            },
            "required": ["year", "month", "day"],
        },
    },
]

HANDLERS = {
    "get_today": lambda args: get_today(),
    "get_week": lambda args: get_week(args["year"], args["month"], args["day"]),
    "days_until": lambda args: days_until(args["year"], args["month"], args["day"]),
}


# ── JSON-RPC 消息循环 ────────────────────────────────

def send_response(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", {})

        # initialize 握手
        if method == "initialize":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "calendar-mcp", "version": "1.0"},
                    "capabilities": {"tools": {}},
                },
            })
            initialized = True
            continue

        # initialized 通知（无 id 的通知，忽略）
        if method == "notifications/initialized":
            continue

        if not initialized:
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "Not initialized"},
            })
            continue

        # tools/list
        if method == "tools/list":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })
            continue

        # tools/call
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            handler = HANDLERS.get(tool_name)
            if handler is None:
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                })
                continue
            try:
                result = handler(arguments)
                if isinstance(result, list):
                    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in result)
                else:
                    text = json.dumps(result, ensure_ascii=False, indent=2)
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}]
                    },
                })
            except Exception as e:
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(e)},
                })
            continue

        # 未知方法
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


if __name__ == "__main__":
    main()
