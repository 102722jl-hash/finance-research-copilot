# mcp_server_extra.py —— v5 第 3 讲：第二个 MCP server（一个"小而独立"的服务）。
#
# 【它存在的唯一目的：证明"可插拔"是真的】
#   客户端 `mcp_client.py` 里，把这行配置加进去：
#       "日历": {"transport": "stdio", "command": PYTHON, "args": [..., "mcp_server_extra.py"]}
#   **客户端其它代码一个字没改**，工具列表里就多出了这个 server 的工具。
#   这就是 MCP 相对"改 tools=[...]" 的质变。
#
# 【它还顺手示范了另一件事：MCP server 可以完全自包含】
#   本文件**不 import 项目里任何模块**（不碰 MySQL、不碰 config、不碰向量库），
#   只用标准库。→ 说明一个 MCP server 是一个**独立可部署的单元**：
#   你可以用别的语言写、可以单独打包、可以部署到另一台机器，客户端完全不用知道。

import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("交易日历")


def _市场状态(现在: datetime) -> str:
    if 现在.weekday() >= 5:
        return "休市（周末）"
    分钟 = 现在.hour * 60 + 现在.minute
    if 分钟 < 9 * 60 + 15:
        return "未开盘"
    if 分钟 < 9 * 60 + 30:
        return "集合竞价中"
    if 分钟 < 11 * 60 + 30:
        return "交易中（早盘）"
    if 分钟 < 13 * 60:
        return "午间休市"
    if 分钟 < 15 * 60:
        return "交易中（午盘）"
    return "已收盘"


@mcp.tool(name="market_clock")
def 现在几点() -> str:
    """返回当前日期时间，以及 A 股市场此刻处于什么状态（未开盘/交易中/已收盘/周末休市）。

    什么时候该用：用户问"今天""现在"，或需要判断**行情数据是不是实时的**时，
    先用它确定"现在"是什么时候、市场开没开。
    """
    现在 = datetime.now()
    星期 = "一二三四五六日"[现在.weekday()]
    时刻 = 现在.strftime("%Y-%m-%d %H:%M:%S")
    return (f"当前时间：{时刻}（周{星期}）\n"
            f"A 股市场状态：{_市场状态(现在)}\n"
            f"数据源：本机系统时钟（非行情源，仅供判断『数据时点』用）")


@mcp.tool(name="days_since")
def 距今几天(日期: str) -> str:
    """算某个日期距今天有多少天。用于"这是多少天前的事""财报发布距现在多久"这类问题。

    参数：
      - 日期：格式 YYYY-MM-DD，如 "2026-10-13"。
    """
    try:
        目标 = datetime.strptime(日期.strip(), "%Y-%m-%d").date()
    except Exception:
        return f"日期格式认不出：「{日期}」。请用 YYYY-MM-DD 格式。"
    差 = (datetime.now().date() - 目标).days
    if 差 == 0:
        return f"{日期} 就是今天。"
    if 差 > 0:
        return f"{日期} 距今 {差} 天（过去）。"
    return f"{日期} 距今天还有 {-差} 天（未来）。"


if __name__ == "__main__":
    print("[交易日历] MCP server 启动（stdio）", file=sys.stderr)
    mcp.run()
