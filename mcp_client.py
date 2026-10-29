# mcp_client.py —— v5 第 3 讲：客户端。连上 MCP server，**动态发现**工具并直接可用。
#
# 【本文件要证明的一件事】
#   **工具的"清单"不是写死在客户端里的** —— 它是连上 server 后问 `tools/list` 拿回来的。
#   （唯一提到名字的地方是②里为了演示"真调一下"而按名取一个工具；
#     真正跑 Agent 的④完全不提名字，工具列表直接喂给模型。）
#   → 服务端**加一个新工具，客户端一行都不用改**。这就是"可插拔"。
#
# 【和前面版本对比】
#   v1~v5-2：加工具 = 写 @tool + 改 graph.py 的 tools=[...] + 重启（**改 Agent 的代码**）
#   v5-3   ：加工具 = 改 MCP server（**Agent 的代码纹丝不动**）
#
# 【成本】
#   每个工具调用多了一次跨进程 JSON-RPC 往返（本地 stdio 通常 <1ms，可忽略）；
#   代价换来的是"客户端与服务端解耦"。

import asyncio
import json
import os
import sys
import warnings

from langchain_mcp_adapters.client import MultiServerMCPClient

# LangGraph 1.2.14 起 prebuilt 的 create_react_agent 已弃用（迁去 langchain.agents.create_agent），
# 但本环境没装 langchain 包，于是继续用它、只把这条警告静音（仅影响输出美观，不影响功能）。
warnings.filterwarnings("ignore", message=".*create_react_agent has been moved.*")

项目目录 = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable          # 用当前解释器拉起 server，避免"用了另一个 python"

# ══════════════════════════════════════════════════════════════
# ① 描述"去哪找 server" —— 注意：这里只有**连接信息**，没有任何工具信息
# ══════════════════════════════════════════════════════════════
配置 = {
    "投研": {
        "transport": "stdio",                     # 本地子进程 + 管道（还有 streamable-http）
        "command": PYTHON,
        "args": [os.path.join(项目目录, "mcp_server.py")],
        # FASTMCP_LOG_LEVEL=ERROR：把中文工具名的 SEP-986 警告压掉（它只是警告，不影响功能）
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_LOG_LEVEL": "ERROR"},
    },
    # ⭐ 挂第二个 server：客户端**其它代码一个字没改**，工具列表自动变多。
    "日历": {
        "transport": "stdio",
        "command": PYTHON,
        "args": [os.path.join(项目目录, "mcp_server_extra.py")],
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_LOG_LEVEL": "ERROR"},
    },
    # ── v5 第 13 讲：再挂两个**零项目依赖**的 server ──
    #   它们各自独立进程：依赖挂了/没配 Key，都不影响上面那两个（这就是可插拔的兑现）。
    #   加完这里，graph.py 会自动多出 web_search / query_lpr / query_cpi 三个工具，
    #   **Agent 的代码一行都没改**。
    "网页搜索": {
        "transport": "stdio",
        "command": PYTHON,
        "args": [os.path.join(项目目录, "search_mcp_server.py")],
        # TAVILY_API_KEY 由 config._load_env() 从 .env 读；这里把 .env 带过去即可
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_LOG_LEVEL": "ERROR"},
    },
    # v5 第 15 讲把这个 server 从「宏观数据」改名为「外部数据」：
    #   它现在管两类**外部来源**的东西 —— 宏观指标（LPR/CPI）+ **公开财报（合并报表口径）**。
    "外部数据": {
        "transport": "stdio",
        "command": PYTHON,
        "args": [os.path.join(项目目录, "macro_mcp_server.py")],
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "FASTMCP_LOG_LEVEL": "ERROR"},
    },
}


async def 拿工具():
    """连上 server，问它"你有哪些工具"，拿回来直接用。"""
    客户端 = MultiServerMCPClient(配置)
    return await 客户端.get_tools()


def _参数结构(t) -> dict:
    """MCP 工具的参数 schema 可能是 pydantic 模型，也可能已经是 dict —— 统一成 dict。"""
    s = t.args_schema
    if isinstance(s, dict):
        return s
    try:
        return s.model_json_schema()
    except Exception:
        return {}


async def 演示():
    客户端 = MultiServerMCPClient(配置)

    print("=" * 70)
    print("① tools/list —— 动态发现的工具（客户端零 hardcode）")
    工具们 = await 客户端.get_tools()
    for t in 工具们:
        描述 = (t.description or "").strip().splitlines()[0][:60]
        结构 = _参数结构(t)
        属性 = 结构.get("properties", {})
        必填 = 结构.get("required", [])
        签名 = "、".join(名 + ("" if 名 in 必填 else "?") for 名 in 属性)
        print(f"  ▸ {t.name}({签名})")
        print(f"      {描述}")
    print(f"\n  共发现 {len(工具们)} 个工具")
    print("  ⚠️ 注意：客户端里没有任何一个工具名是写死的 —— 全是从两个 server 『问』回来的。")

    print("=" * 70)
    print("② tools/call —— 真的把工具调起来（跨进程！）")
    按名 = {t.name: t for t in 工具们}
    out = await 按名["query_realtime_quote"].ainvoke({"标的": "宝钢股份"})
    print("  " + str(out).replace("\n", "\n  ")[:400])

    print("=" * 70)
    print("③ resources/read —— 读『资料』（不是函数调用，是取一份文档）")
    try:
        资料 = await 客户端.get_resources("投研")
        for b in 资料:                       # 适配器把资源读回来是 Blob（bytes + metadata）
            元 = getattr(b, "metadata", {}) or {}
            正文 = b.data.decode("utf-8", "ignore") if isinstance(b.data, (bytes, bytearray)) else str(b.data)
            前几行 = "\n".join(正文.splitlines()[:6])
            print(f"  ▸ uri={元.get('uri')}")
            print("    " + 前几行.replace("\n", "\n    "))
    except Exception as e:
        print("  （读资源失败）", type(e).__name__, e)

    print("=" * 70)
    print("④ prompts/get —— 取回一个『提示词模板』（由用户选用，不是模型自己调）")
    try:
        消息 = await 客户端.get_prompt("投研", "stock_brief", arguments={"标的": "宝钢股份"})
        for m in 消息:
            print(f"  ▸ [{type(m).__name__}] {str(m.content)[:200]}")
    except Exception as e:
        print("  （取模板失败）", type(e).__name__, e)

    return 工具们


async def 跑一个Agent(问题: str):
    """用发现的工具现场搭一个 ReAct Agent —— 这段代码对工具一无所知。"""
    from langchain_core.messages import SystemMessage
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    工具们 = await 拿工具()
    模型 = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL, temperature=0)
    智能体 = create_react_agent(
        模型, 工具们,
        prompt=SystemMessage(content=(
            "你是金融投研助手。只用工具返回的数据作答；"
            "行情问题必须照搬数据源与快照时点；公告引用必须标文件名与页码。")))

    最终 = None
    async for 事件 in 智能体.astream({"messages": [("user", 问题)]}, stream_mode="updates"):
        for 节点, 更新 in 事件.items():
            摘要 = ""
            for m in (更新 or {}).get("messages", []):
                tc = getattr(m, "tool_calls", None)
                if tc:
                    摘要 = f"要调 {tc[0]['name']}({tc[0]['args']})"
                elif getattr(m, "content", ""):
                    摘要 = str(m.content)[:110].replace("\n", " ")
                    最终 = m
            print(f"  【{节点}】{摘要}")
    return 最终.content if 最终 else "（无）"


if __name__ == "__main__":
    print("（说明：下面形如 `[投研工具集] MCP server 启动` 与 "
          "`INFO Processing request of type ListToolsRequest` 的行，"
          "是 **server 子进程打到 stderr** 的日志 —— 它正好证明 JSON-RPC 真的跨进程在跑。"
          "stdio 模式下 stdout 是协议通道，所以这些日志只能走 stderr。）", file=sys.stderr)
    问题 = " ".join(sys.argv[1:]).strip()
    if 问题:
        print("=" * 70)
        print(f"④ 用 MCP 工具跑 Agent：{问题}")
        print("=" * 70)
        print(asyncio.run(跑一个Agent(问题)))
    else:
        asyncio.run(演示())
