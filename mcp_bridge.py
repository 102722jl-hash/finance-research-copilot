# mcp_bridge.py —— 把 **异步的 MCP 工具** 桥接给 **同步的 graph.py**（v5 第 13 讲）
#
# 【为什么需要这个文件】
#   `graph.py` 是同步代码：`tools = [...]`、`llm.bind_tools(tools)`，import 期就定好。
#   而 MCP 客户端是异步的：拿工具 `await client.get_tools()`、调工具 `await t.ainvoke()`。
#   两条时间线对不上 → 必须有个桥。
#
# 【桥的三要素】
#   ① **常驻事件循环**：后台线程跑 `loop.run_forever()`，所有协程往它上面丢；
#   ② **常驻 MCP 会话**：子进程不能每调一次就重启（实测**每次重启约 3 秒**）。
#      做法是每个 server 派一个"永不返回"的任务，把自己卡在 `async with client.session(名)` 里；
#   ③ **缓存**：工具列表只取一次，之后复用。
#
# 【降级】
#   MCP 是**外部依赖**（起子进程 + 走网络）。它挂了绝不能让 `graph.py` import 失败
#   —— 那是整个服务起不来。所以：取不到就返回 `[]`，并在 stderr 记一行。
#
# ⚠️ 【踩坑：async with MultiServerMCPClient(...) 不能用】
#   langchain-mcp-adapters ≥ 0.1.0 直接抛 NotImplementedError：
#       "MultiServerMCPClient cannot be used as a context manager"
#   官方给的两种正确姿势：
#       1) client = ...; await client.get_tools()                       ← 一次性，用完即弃（每调重启子进程）
#       2) async with client.session(name) as s: await load_mcp_tools(s) ← **会话可持久** ← 我们用这个
#
# ⚠️ 【第二个坑】不能用 `await asyncio.gather(*保持任务)` —— 那些任务**故意永不返回**，
#   gather 会永远挂住，工具列表就拿不回来。正确做法：用 `create_task` 派出去（不 await），
#   然后轮询"工具收集够了没"。

import asyncio
import os
import sys
import threading
import time

# ══════════════════════════════════════════════════════════════
# 🔴 v5 第 15 讲：**必须把 BLAS/OpenMP 线程数钉成 1**（这一行救过一次线上启动失败）
# ══════════════════════════════════════════════════════════════
# 【现象】回归/服务在 L2 层挂掉一半 MCP 工具，日志里是：
#     OpenBLAS error: Memory allocation still failed after 10 retries, giving up.
#     McpError: Connection closed        ← 子进程**启动即死**
#   已发现的工具只剩 ['days_since', 'market_clock', 'web_search']（`投研`/`外部数据` 全没了）。
#
# 【根因】OpenBLAS 默认按 **CPU 核数** 给每个线程预分配一块缓冲。
#   本机是 8 核 16 线程 → 一个进程就能要一大块；而我们有 **4 个 MCP 子进程**
#   + 主进程（langchain / numpy / akshare 一起上）→ 内存瞬间被吃穿，子进程起不来。
#
# 【为什么这样修是对的】这些工具都是**单次、短小**的调用（查一次数、取一张表），
#   根本不需要多线程 BLAS —— 线程数钉 1 对性能**没有实际影响**，却让启动变确定。
#   `setdefault`：用户若显式设过，尊重用户的。
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

# 用环境变量关掉 MCP（排查问题时很有用）：MCP_ENABLED=0
启用 = os.getenv("MCP_ENABLED", "1") not in ("0", "false", "False", "")

# 启动超时：拉起 N 个 server 子进程要几秒；但**有的 server 会在启动时加载重依赖**
# （实测 `宏观数据` server 启动时要 import akshare，慢的一次要 40+ 秒）→ 给足余量。
启动超时 = float(os.getenv("MCP_BOOT_TIMEOUT", "120"))

_环: asyncio.AbstractEventLoop | None = None
_环锁 = threading.Lock()

_工具: list | None = None        # 缓存：工具列表只取一次
_就绪 = threading.Event()
_失败: str | None = None

# 单次工具调用超时（akshare 那类接口偶尔会慢，给足一点）
工具超时 = float(os.getenv("MCP_TOOL_TIMEOUT", "60"))


def _拿环() -> asyncio.AbstractEventLoop:
    """单例后台事件循环（daemon 线程，随进程退出）。"""
    global _环
    with _环锁:
        if _环 is None:
            _环 = asyncio.new_event_loop()
            threading.Thread(target=_环.run_forever, daemon=True,
                             name="mcp-bridge-loop").start()
        return _环


async def _保持一个会话(客户端, 服务名: str, 收集: list, 完成: set) -> None:
    """卡在会话上下文里永不返回 —— 子进程和 session 就一直活着。"""
    from langchain_mcp_adapters.tools import load_mcp_tools
    async with 客户端.session(服务名) as 会话:
        收集.extend(await load_mcp_tools(会话))
        完成.add(服务名)
        await asyncio.Event().wait()      # 永久挂起（直到进程退出）


async def _启动(配置: dict) -> list:
    """派发常驻任务，等工具收集齐就返回（任务继续在后台活着）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    收集: list = []
    完成: set = set()
    客户端 = MultiServerMCPClient(配置)

    for 名 in 配置:
        # ⚠️ create_task，**不要 await**（这些任务故意永不返回）
        asyncio.create_task(_保持一个会话(客户端, 名, 收集, 完成),
                            name=f"mcp-session-{名}")

    截止 = time.time() + 启动超时
    while len(完成) < len(配置) and time.time() < 截止:
        await asyncio.sleep(0.05)
    return 收集


def _后台启动() -> None:
    global _工具, _失败
    try:
        from mcp_client import 配置
        未来 = asyncio.run_coroutine_threadsafe(_启动(配置), _拿环())
        _工具 = 未来.result(启动超时 + 5)
        if not _工具:
            _失败 = "没有拿到任何 MCP 工具"
    except Exception as e:
        _失败 = f"{type(e).__name__}: {e}"
        _工具 = None
    finally:
        _就绪.set()


def _文本化(out) -> str:
    """MCP 工具返回的是 content 块列表（[{'type':'text','text':…}]），压成一段话。"""
    if isinstance(out, list):
        parts = []
        for x in out:
            if isinstance(x, dict):
                parts.append(x.get("text", str(x)))
            else:
                parts.append(getattr(x, "text", None) or str(x))
        return "\n".join(parts)
    return str(out)


def _参数结构(t) -> dict:
    """MCP 工具的参数 schema 可能是 pydantic 模型，也可能已经是 dict —— 统一成 dict。"""
    s = t.args_schema
    if isinstance(s, dict):
        return s
    try:
        return s.model_json_schema()
    except Exception:
        return {}


def 包成同步工具(t):
    """把一个**异步**的 MCP StructuredTool 包成**同步可调用**的（🔴 关键一步）。

    🔴 【踩坑：MCP 工具不能直接塞进同步图】
       `langchain_mcp_adapters` 给回来的是 `StructuredTool`，它**只有 coroutine**，
       同步 `.invoke()` 会抛：
           NotImplementedError: StructuredTool does not support sync invocation.
       而 `graph.py` 是同步图（ToolNode 走同步分支）→ 一调到 MCP 工具就炸。

       解法：**按需包一层同步壳** —— 外壳的 `func` 是普通同步函数，
       内部用背景事件循环跑真正的 `ainvoke()`。对上层的图/模型来说，
       它和进程内的 `@tool` 长得一模一样。
    """
    from langchain_core.tools import StructuredTool

    结构 = _参数结构(t)

    def _同步(**kwargs):
        fut = asyncio.run_coroutine_threadsafe(t.ainvoke(kwargs), _拿环())
        try:
            return _文本化(fut.result(工具超时))
        except TimeoutError:
            return f"【{t.name}】调用超时（{工具超时}s），请稍后重试。"
        except Exception as e:
            # 工具绝不能抛异常（会炸穿 ToolNode → 图 → 接口），一律退化成人话
            return f"【MCP 工具 {t.name} 调用失败】{type(e).__name__}: {e}"

    return StructuredTool(
        name=t.name,
        description=t.description or t.name,
        args_schema=结构 or None,
        func=_同步,          # ⚠️ 只给 func、不给 coroutine → 同步图就能调它
    )


def 同步拿工具() -> list:
    """同步地拿到所有 MCP 工具（带缓存 + 降级 + 同步包装）。

    - 首次调用拉起 server 子进程（约 2~4s），之后直接返回缓存；
    - 失败一律返回 `[]`，**绝不抛异常**（外部依赖不能拖垮主流程）。
    """
    global _工具
    if not 启用:
        return []
    if not _就绪.is_set():
        threading.Thread(target=_后台启动, daemon=True,
                         name="mcp-bridge-boot").start()
        if not _就绪.wait(启动超时 + 5):
            print(f"[mcp_bridge] ⚠️ MCP 工具加载超时（{启动超时}s），降级为不使用 MCP",
                  file=sys.stderr)
            return []
    if _失败:
        print(f"[mcp_bridge] ⚠️ MCP 不可用（{_失败}），降级为不使用 MCP",
              file=sys.stderr)
    return [包成同步工具(t) for t in (_工具 or [])]


def 同步拿原始工具() -> list:
    """拿未经包装的 MCP 工具（测试/诊断时用，直接 ainvoke 更方便）。"""
    同步拿工具()
    return _工具 or []


def 工具名们() -> list[str]:
    return [t.name for t in 同步拿工具()]


if __name__ == "__main__":
    t0 = time.time()
    工具们 = 同步拿工具()
    print(f"① 首次取工具 {time.time()-t0:.2f}s，共 {len(工具们)} 个（已包成同步可调用）：")
    for t in 工具们:
        print(f"   ▸ {t.name}")

    print("\n② 同步调用测试（同步图里就是这么调的）：")
    按名 = {t.name: t for t in 工具们}
    for 名, 参数 in [("query_lpr", {}), ("query_cpi", {}),
                     ("web_search", {"query": "钢铁行业 最新政策", "max_results": 3})]:
        if 名 not in 按名:
            print(f"   【{名}】⚠️ 不在工具列表里")
            continue
        t1 = time.time()
        try:
            out = 按名[名].invoke(参数)          # ← **同步** .invoke()，图里就是这么用的
            print(f"   【{名}】{time.time()-t1:.2f}s")
            print("     " + str(out).replace("\n", "\n     ")[:500])
        except Exception as e:
            print(f"   【{名}】❌ {type(e).__name__}: {e}")
