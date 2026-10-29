# server.py —— v5 第 2 讲：把 Copilot 变成一个 HTTP 服务（FastAPI + SSE 流式）
#
# 【为什么要服务化】
#   前面 4 个版本都是"脚本"：`python run.py "问题"` 跑一次、人肉看输出。
#   服务化要解决的是**别人（前端/同事/另一个程序）怎么用你**：
#     - 统一的入口和契约（HTTP + JSON）
#     - 会话不再靠"进程活着"，而是靠 **session_id**（可持久、可复现）
#     - 流式：首字延迟从"整段生成完"降到"模型吐第一个字"
#
# 【本讲的 8 个知识点（讲义里有逐条讲解 + 实测）】
#   1. **图必须建成进程级单例** —— 每请求 build_graph() 会重编译、并且丢掉 checkpointer 里所有会话。
#   2. **session_id ↔ thread_id**：项目自带评测集 `clean.xlsx` 里的 session_id 就是它。
#   3. **SSE 而不是 WebSocket**：单向、纯 HTTP、自动重连、不用握手。
#      但 EventSource 只支持 GET → 我们的接口是 POST + JSON，所以前端要用 `fetch` 读流。
#   4. **流的两种粒度**：节点级 `stream_mode="updates"`（进度）+ token 级 `"messages"`（逐字）。
#   5. **interrupt 在 HTTP 上怎么表达**：流里发一个 `interrupt` 事件 → 前端收集答复 → 调 `/chat/resume`。
#   6. **同步节点会不会阻塞事件循环？实测：不会**（LangGraph 自动把同步节点丢线程池）。
#      3 并发 2.42s ≈ 单个 2.35s，同步/异步两种写法**几乎无差别**（见讲义实测表）。
#   7. **错误不能把连接炸掉**：工具抛异常要包成一条"如实说"的事件，而不是 500。
#   8. **还没做的生产化**：鉴权、限流、日志/追踪、Docker、持久化 checkpointer（现在还是进程内的）。

import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel

import guard                                   # v5-5：断言溯源（给响应体挂指标）
from graph import build_graph

# ══════════════════════════════════════════════════════════════
# ① 进程级单例（知识点 1）
#    这个模块被 import 一次，图就建一次。所有请求共用同一个图 → 共用同一批会话。
#    ⚠️ 如果写在端点函数里 build_graph()，每次请求都会：
#       ① 重新编译图（慢）② 拿一个全新的 checkpointer（**上一条会话历史全丢**）
# ══════════════════════════════════════════════════════════════
检查点 = InMemorySaver()
偏好库 = InMemoryStore()
图谱 = build_graph(checkpointer=检查点, store=偏好库)

# 给评测演示用：默认偏好（真实系统里应按 user_id 分命名空间）
偏好命名空间 = ("偏好", "me")
# ⚠️ 兼容修复：不同 langgraph 版本对 InMemoryStore.put 的 value 要求不同 ——
#    老版（本项目锁的 1.2.14）任意值都行，可存字符串；新版**强制 dict[str, Any]**，
#    存字符串会在 `dict(op.value)` 抛 ValueError。而这行是 **import 期**执行 →
#    一旦抛错，整个服务（uvicorn）直接起不来。
#    → 先试 dict（新版），失败退回字符串（老版）；都不行也**不阻断启动**。
#      （这只是"演示种子数据"，读不到偏好只是少一条软约束，不该把服务赔进去。）
def _播种演示偏好() -> None:
    for 值 in ({"文本": "金额一律换算成亿元，保留两位小数。"},
               "金额一律换算成亿元，保留两位小数。"):
        try:
            偏好库.put(偏好命名空间, "展示", 值)
            return
        except Exception:
            continue


_播种演示偏好()

app = FastAPI(title="金融投研 Copilot", version="v5-2")


# ══════════════════════════════════════════════════════════════
# ② 契约（Pydantic 模型）—— 服务化最重要的一步是"把接口定死"
# ══════════════════════════════════════════════════════════════
class 提问(BaseModel):
    question: str
    session_id: str | None = None          # 不传 → 服务端发一个；传 → 续上这轮会话


class 恢复(BaseModel):
    session_id: str
    answer: str


def _新会话() -> str:
    return uuid.uuid4().hex[:12]


def _配置(会话: str) -> dict:
    """session_id → thread_id（知识点 2）。

    v5-5 新增 `recursion_limit`：图的**外层步数上限**。
      ⚠️ 它和 graph.py 里的"轮数护栏"是**两层**，不是一回事：
         · 轮数护栏（最大工具轮数=3）管的是**业务**——"连着查 3 轮还没收敛就该认输"；
         · recursion_limit 管的是**框架**——兜底，防止任何我没预料到的环。
      LangGraph 的默认值偏高（25），乘上每轮 1~2 次 LLM 调用，就是 v5-4 里
      那 3 条"服务端 150s 不返回"的直接来源。这里压到 12。
    """
    return {"configurable": {"thread_id": 会话}, "recursion_limit": 12}


# 服务端硬超时（秒）。客户端之外再兜一层是刻意的：
#   v5-4 的教训是"客户端 60s 超时，服务端还在跑" —— 资源没释放，连接还挂着。
#   生产里这一层等于"主动放弃"，比"等它自己死"省资源。
# v5-5 从 60 收到 45。
#
# ⚠️ v5-6 更正一处**错误归因**：v5-5 这里写的是"兜单次工具本身很慢"——**这是错的**。
#   v5-6 实测：检索() 整条仅 211ms（embed 59 + chroma 4 + bm25 3 + rerank 139），工具根本不慢。
#   真正的耗时源是**推理模型的隐藏 thinking**（耗时 ≈ 输出token ÷ 150，单次调用可吐几千 token）。
#   → 所以这一层兜的是"模型生成太慢/多轮叠加"，不是工具。治本手段是关思考 + 流式（见 v5 第 6 讲）。
#   45s 作为兜底保留：关思考后单次调用约 8s，正常请求远低于此，不应误杀。
端到端超时 = 45.0


def _最后回答(消息们: list) -> str:
    """取最后一条"有内容"的 AIMessage —— 和 run.py 里同一套逻辑（复核拦截时会用拦截消息）。"""
    for m in reversed(消息们 or []):
        if isinstance(m, AIMessage) and (m.content or "").strip():
            return m.content
    return ""


# ══════════════════════════════════════════════════════════════
# ⑨ 【v5-4 新增】成本口径：把 token 用量从消息里抠出来
#    —— 评测要的"成本"这一列，靠的就是每个 AIMessage 上的 usage_metadata。
#    ⚠️ 它是**由模型服务返回的**，不是我们算的：OpenAI 兼容端点通常都会带
#       {input_tokens, output_tokens, total_tokens}。若端点没返回（少数自建网关），
#       这里全 0 → 于是我们**同时**记一份字符数当兜底估算口径，避免整张成本表变空。
# ══════════════════════════════════════════════════════════════
def _用量(消息们: list) -> dict:
    总 = {"input": 0, "output": 0, "total": 0, "chars": 0, "llm_calls": 0}
    for m in 消息们 or []:
        if not isinstance(m, AIMessage):
            continue
        总["llm_calls"] += 1
        总["chars"] += len(m.content or "")
        u = getattr(m, "usage_metadata", None)
        if isinstance(u, dict):
            总["input"] += u.get("input_tokens") or 0
            总["output"] += u.get("output_tokens") or 0
            总["total"] += u.get("total_tokens") or 0
    return 总


def _工具(消息们: list) -> list:
    """【v5-8 新增】本轮**实际调用了哪些工具**（按名字）。

    为什么需要它：`steps` 只给**节点名**（call_model / tools / 复核…），看不出模型到底选了哪个工具。
    评测里"选对工具"和"没选工具"必须分开 —— 比如：
      · "今天有多少涨停股票" → 应该看到 `查涨停池`；
      · 什么都没调却给了数字 → 幻觉风险信号（v5-4 的老判据）。
    实现：ToolMessage 上就带着 `.name`（LangChain 自动填的工具名）。
    """
    return [getattr(m, "name", "") or "" for m in (消息们 or []) if isinstance(m, ToolMessage)]


def _sse(类型: str, 数据: dict) -> str:
    """SSE 的一帧：`data: <json>\\n\\n`（知识点 3）。空行是**帧结束**的标志，不能省。"""
    return f"data: {json.dumps({'type': 类型, **数据}, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════════════
# ⑩ 【v5-5 新增】三个可自动统计的指标：通道 / 无支撑断言率 / 越界命中
#    —— 这是"把'不编造'变成可测量"的那一步：
#       以前只能说"我用了护栏"，现在能说"我的无支撑断言率是 0%"。
# ══════════════════════════════════════════════════════════════
def _指标(本轮: list, 问题: str, 会话消息: list | None = None) -> dict:
    回答 = _最后回答(本轮)
    # ⚠️ 护栏文案本身不算"实质回答"，必须排除在指标之外 —— 这是实测踩到的坑：
    #    第一次跑批时，「归母净利润」那条被"认输"拦下，拒答文案里写着
    #    "已连续调用工具 3 轮（上限 3）"，于是指标算出「断言 2、无支撑 2、无支撑断言率 100%」。
    #    这不是模型在编，是**我们在量自己写的文案**。指标口径必须先排除拒答。
    if "【无法回答】" in 回答 or "【护栏拦截】" in 回答:
        return {"无支撑断言率": 0.0, "断言数": 0, "无支撑数": 0, "越界命中": [],
                "无支撑样例": [], "护栏": True}
    # ⚠️ v5-6 修第二个"指标口径"坑：**支撑池不能只看本轮**。
    #    实测：连续两轮打同一批 session，第二轮模型直接从上一轮上下文里拿到了数据、
    #    不再调用工具（工具轮数=0），于是"本轮工具原文"为空 → 答案里每个数字都被判无支撑，
    #    无支撑断言率虚报 100%（关联交易 65/65）。这是**假警报**。
    #    语义上正确的支撑池是"**本会话迄今所有工具原文**"—— 第 2 轮复用第 1 轮查到的数据，
    #    本就是多轮对话的正常行为，不该算编造。
    #    （单轮场景下 会话消息 == 本轮，行为与 v5-5 完全一致，不影响已发布的基线数字。）
    支撑源 = 会话消息 if 会话消息 is not None else 本轮
    工具原文 = "\n".join(m.content or "" for m in 支撑源 if isinstance(m, ToolMessage))
    指标 = guard.评估回答(回答, 工具原文, 问题)
    return {"无支撑断言率": 指标["无支撑断言率"], "断言数": 指标["断言数"],
            "无支撑数": 指标["无支撑数"], "越界命中": 指标["越界命中"],
            # 回传样例是为了**能自证**：报告里那些"无支撑"到底是模型编的，
            # 还是我们的匹配器误报 —— 光看一个比例是分不出来的。
            "无支撑样例": 指标["无支撑样例"], "护栏": False}


def _超时答复(会话: str, 起点: float, 轨迹: list, 原因: str) -> dict:
    """超时 / 超出递归上限时的**统一出口**：也是"回答"，只是内容是"我为什么答不了"。

    ⚠️ 关键差别：v5-4 这两种情况返回的是 `status: "error"`（接口层的失败），
      现在返回的是 `status: "ok"` + 一条结构化拒答 —— 因为从**业务**上看，
      "我查不出来"本来就是一次有效的回答。响应率才算得上 100%。
    """
    return {"session_id": 会话, "status": "ok", "answer": guard.拒绝文案(原因),
            "steps": 轨迹, "通道": "—",
            "usage": {"input": 0, "output": 0, "total": 0, "chars": 0, "llm_calls": 0},
            "无支撑断言率": 0.0, "断言数": 0, "无支撑数": 0, "越界命中": [],
            "无支撑样例": [], "guard": 原因, "护栏": True,
            "elapsed_ms": int((time.perf_counter() - 起点) * 1000)}


# ══════════════════════════════════════════════════════════════
# ③ 非流式问答：一次返回完整结果（适合脚本调用 / 评测跑批）
# ══════════════════════════════════════════════════════════════
@app.post("/chat")
async def 对话(体: 提问):
    会话 = 体.session_id or _新会话()
    起点 = time.perf_counter()
    # 记下"跑之前有多少条消息"，跑完做差 → 才能只算**本轮**的 token（state 是累积的）
    前 = len((图谱.get_state(_配置(会话)).values or {}).get("messages") or [])
    轨迹 = []
    try:
        # v5-5：三层"别卡死"里的最外层 —— 服务端硬超时。
        #   内两层是 轮数护栏（graph.py）和 recursion_limit（_配置）。
        #   为什么还要这一层？因为它们都防不住"单次工具调用本身很慢"（外部接口挂了、
        #   数据库在重连）—— 那种情况轮数根本没涨，只能靠墙上的钟。
        async with asyncio.timeout(端到端超时):
            async for 事件 in 图谱.astream({"messages": [("user", 体.question)]},
                                        _配置(会话), stream_mode="updates"):
                for 节点, 更新 in 事件.items():
                    if 节点 == "__interrupt__":
                        return {"session_id": 会话, "status": "interrupted",
                                "question": 更新[0].value, "steps": 轨迹,
                                "elapsed_ms": int((time.perf_counter() - 起点) * 1000)}
                    轨迹.append(节点)
    except TimeoutError:                               # 超时 → 结构化拒答，不是 500
        return _超时答复(会话, 起点, 轨迹, "超时")
    except GraphRecursionError:                        # 步数兜底 → 同样走拒答
        return _超时答复(会话, 起点, 轨迹, "轮数上限")
    except Exception as e:                             # 知识点 7：别把异常原样抛给调用方
        return {"session_id": 会话, "status": "error",
                "error": f"{type(e).__name__}: {e}", "steps": 轨迹,
                "elapsed_ms": int((time.perf_counter() - 起点) * 1000)}

    状态 = 图谱.get_state(_配置(会话))
    本轮 = (状态.values.get("messages") or [])[前:]
    return {"session_id": 会话, "status": "ok",
            "answer": _最后回答(状态.values.get("messages")), "steps": 轨迹,
            "通道": 状态.values.get("通道"),                        # ← v5-5 新增：分诊结果
            "子类": 状态.values.get("子类") or "",                   # ← v5-17 续：自述 / 域外（给前端区分子通道）
            "工具": _工具(本轮),                                     # ← v5-8 新增：本轮调了哪些工具
            "usage": _用量(本轮),                                   # ← v5-4 新增：成本
            **_指标(本轮, 体.question, 状态.values.get("messages")),  # ← v5-6：支撑池=本会话全部工具原文
            "elapsed_ms": int((time.perf_counter() - 起点) * 1000)}  # ← v5-4 新增：时延


# ══════════════════════════════════════════════════════════════
# ④ 流式问答（SSE）
#    ⚠️ `stream_mode=["updates","messages"]` 是"多模式"写法：
#       每个事件是 `(模式名, 数据)` 元组 —— 这是实测确认的形状（见讲义）。
# ══════════════════════════════════════════════════════════════
# 【本地修复】只允许这两个节点的 LLM token 出现在前端正文里：
#   · call_model —— 主作答（含带工具的多轮）
#   · 收尾       —— 超轮后基于已有数据尽力作答（此时 call_model 没产出最终文案，必须流它）
可流节点: frozenset[str] = frozenset({"call_model", "收尾"})


@app.post("/chat/stream")
async def 流式对话(体: 提问):
    会话 = 体.session_id or _新会话()
    起点 = time.perf_counter()
    前 = len((图谱.get_state(_配置(会话)).values or {}).get("messages") or [])

    async def 事件流():
        yield _sse("start", {"session_id": 会话, "question": 体.question})
        轨迹 = []
        try:
            # v5-5：流式这条路径同样要有硬超时 —— 否则"卡死"只是从非流式挪到了流式，
            #       前端会看到 token 逐字吐到一半然后永远不动（体感比 500 更糟）。
            async with asyncio.timeout(端到端超时):
                async for 模式, 数据 in 图谱.astream(
                        {"messages": [("user", 体.question)]},
                        _配置(会话), stream_mode=["updates", "messages"]):
                    if 模式 == "updates":
                        for 节点, 更新 in 数据.items():
                            if 节点 == "__interrupt__":
                                yield _sse("interrupt", {"question": 更新[0].value})
                                return
                            轨迹.append(节点)
                            yield _sse("node", {"node": 节点, "index": len(轨迹)})
                    else:
                        # messages 模式既会吐 LLM 的 token 碎片，也会吐 ToolMessage 整条
                        # → 只挑 AIMessageChunk 且内容非空的（知识点 4）
                        #
                        # ⚠️⚠️ 【本地修复 · 真 bug：分诊 JSON 泄进答案】⚠️⚠️
                        #   `stream_mode="messages"` 吐的是**全图所有节点**的 LLM token，
                        #   不只是 call_model —— 分诊节点内部也调了一次 LLM（要求它输出
                        #   `{"通道":"A","理由":"..."}` 的 JSON），这段 JSON 被当正文推给了前端，
                        #   用户看到的答案开头就多出一行 `{"通道":"A","理由":"有明确标的 + 明确信息类型"}`。
                        #   ⇒ 必须按 `metadata["langgraph_node"]` 过滤：**只流"最终作答"的节点**。
                        #     白名单 = call_model（主作答）+ 收尾（超轮后基于已有数据收口作答）。
                        #     分诊 / 复核 / 溯源 / 拒答 的 LLM 输出一律不流。
                        块, 元 = 数据
                        if (isinstance(块, AIMessageChunk) and isinstance(块.content, str)
                                and 块.content
                                and (元 or {}).get("langgraph_node") in 可流节点):
                            yield _sse("token", {"text": 块.content})
        except (TimeoutError, GraphRecursionError) as e:
            类别 = "超时" if isinstance(e, TimeoutError) else "轮数上限"
            # ⚠️ 用 done 事件而不是 error 事件：对前端来说这是一次**正常完成**，
            #    答案内容是"我为什么答不了"。error 事件留给真正的接口级故障。
            yield _sse("done", {"answer": guard.拒绝文案(类别), "steps": 轨迹,
                                "session_id": 会话, "通道": "—", "guard": 类别,
                                "usage": {"input": 0, "output": 0, "total": 0,
                                          "chars": 0, "llm_calls": 0},
                                "无支撑断言率": 0.0, "断言数": 0, "无支撑数": 0, "越界命中": [],
                                "elapsed_ms": int((time.perf_counter() - 起点) * 1000)})
            return
        except Exception as e:
            yield _sse("error", {"error": f"{type(e).__name__}: {e}"})
            return

        状态 = 图谱.get_state(_配置(会话))
        本轮 = (状态.values.get("messages") or [])[前:]
        yield _sse("done", {"answer": _最后回答(状态.values.get("messages")),
                            "steps": 轨迹, "session_id": 会话,
                            "通道": 状态.values.get("通道"),                        # ← v5-5 新增
                            "子类": 状态.values.get("子类") or "",                  # ← v5-17 续：自述 / 域外
                            "工具": _工具(本轮),                                    # ← v5-8 新增
                            "usage": _用量(本轮),                                  # ← v5-4 新增
                            **_指标(本轮, 体.question),                            # ← v5-5 新增
                            "elapsed_ms": int((time.perf_counter() - 起点) * 1000)})

    return StreamingResponse(事件流(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})   # 关掉反代缓冲


# ══════════════════════════════════════════════════════════════
# ⑤ 中断恢复（知识点 5）：人工介入在 HTTP 上就两步 —— 收到问题、把答复交回去
# ══════════════════════════════════════════════════════════════
@app.post("/chat/resume")
async def 恢复对话(体: 恢复):
    try:
        out = await 图谱.ainvoke(Command(resume=体.answer), _配置(体.session_id))
    except Exception as e:
        return {"session_id": 体.session_id, "status": "error",
                "error": f"{type(e).__name__}: {e}"}
    return {"session_id": 体.session_id, "status": "ok",
            "answer": _最后回答(out.get("messages"))}


@app.get("/history/{session_id}")
async def 历史(session_id: str):
    """读 checkpointer 里的会话（证明"服务"是有状态的，不是无状态转发）。"""
    状态 = 图谱.get_state(_配置(session_id))
    if not 状态.values:
        return {"session_id": session_id, "messages": []}
    return {"session_id": session_id, "messages": [
        {"role": type(m).__name__, "content": (m.content or "")[:200]}
        for m in 状态.values.get("messages", [])]}


# ══════════════════════════════════════════════════════════════
# ⑥ 健康检查：轻量 /health（可轮询） + 深度 /health/deep（人工排查）
#
# 【为什么必须分两个】
#   本项目有三条**会静默降级**的链路，表现都不是报错，而是"质量悄悄掉"：
#     ① `hybrid_search.重排()` 失败 → `except -> return []`，退回 RRF 顺序；
#     ② 语义检索异常 → `tools._语义找()` 返回 []，只是"没匹配到"；
#     ③ MCP 子进程没起来 → 工具从 24 个变 15 个，服务**照常启动**。
#   这三件事以前只能靠"感觉答得不准"来猜。
#   ⇒ /health 只**读进程内状态**（零外部请求，前端可以放心轮询）；
#     /health/deep 才**真打一次精排 + 连一次库**（人工排查用，有成本）。
# ══════════════════════════════════════════════════════════════

# 完整工具集 = 本地 15 + MCP 9。少一个就说明 MCP 那一路没挂上。
期望工具数 = 24


def _主机(url: str) -> str:
    try:
        return url.split("://", 1)[-1].split("/", 1)[0]
    except Exception:
        return "(无法解析)"


def _工具降级(工具们: list[str]) -> list[str]:
    问题 = []
    if len(工具们) < 期望工具数:
        问题.append(
            f"MCP 工具缺失：只有 {len(工具们)}/{期望工具数} 个"
            f"（跑 `python mcp_bridge.py` 看是否打印「共 9 个」；"
            f"头号原因是 langchain-mcp-adapters / akshare / tavily-python 装漏）")
    return 问题


# 厂商实测**不存在**的模型 —— 配上去必然 400，而且只有第一次检索时才会暴露。
# ⚠️ 这是"用确认过的错误挡住同类错误"，和 `guard._歧义候选` 是同一个思路：
#    没法枚举所有合法模型名（那要联网查），但能钉死**已经踩过的坑**。
#    2026-10-20 实测：硅基流动没有 `BAAI/bge-reranker-large`（400 code 20012），只有 v2-m3。
_已知不可用模型 = {
    ("RERANK", "BAAI/bge-reranker-large"): "硅基流动无此模型（实测 400 code 20012），只有 BAAI/bge-reranker-v2-m3",
}

_配置问题缓存: list[str] | None = None


def _配置自检() -> list[str]:
    """**纯本地**的一致性校验：零外部请求，但能挡住"配错了、还没触发"这类问题。

    【为什么非要有它】
      `/health` 只读运行期状态。实测（2026-10-20 故障注入）：
      把 `RERANK_MODEL` 改成不存在的模型再重启，`/health` 依然返回 `ok_all: true` ——
      因为**还没人查过数**，`重排状态.失败次数` 就是 0。
      ⇒ 一个"永远绿"的健康检查比没有更危险：它给的是**假安全感**。
      所以这里补一层静态校验，专门抓"配置层面就错了"的情况。

    【只做本地能确定的事】
      · 已知不可用模型名单（厂商实测过 400 的）
      · 两个向量库的维度必须一致（换 embedding 只重建一半 = 必然维度报错）
      · schema_store 的 mean.npy 必须与 vectors.npy 同维（否则查询端分数不可比）
      需要联网才能判断的（比如"这个模型厂商还提不提供"）交给 `/health/deep`。
    """
    global _配置问题缓存
    if _配置问题缓存 is not None:
        return list(_配置问题缓存)

    import config as _c

    问题: list[str] = []

    # ① 已知不可用模型
    for (类, 模型), 说明 in _已知不可用模型.items():
        当前 = (_c.RERANK_MODEL if 类 == "RERANK" else _c.EMBED_MODEL).strip()
        if 当前 == 模型:
            问题.append(f"{类}_MODEL 配了不存在的模型：{模型}（{说明}）")

    # ② 向量库维度一致性
    try:
        import numpy as _np
        本目录 = os.path.dirname(os.path.abspath(__file__))
        维们: dict[str, int] = {}
        for 名 in ("rag_store", "schema_store"):
            向量文件 = os.path.join(本目录, 名, "vectors.npy")
            if os.path.exists(向量文件):
                维们[名] = int(_np.load(向量文件, mmap_mode="r").shape[1])
        if len(set(维们.values())) > 1:
            问题.append(
                f"两个向量库维度不一致 {维们} —— 换 embedding 时**只重建了一半**，"
                f"查询必然维度报错。必须跑一次 `重建向量库.py`（不带参数，两库一起建）")
        # mean.npy 必须与 schema 库同维，否则查询端减错均值 → 分数不可比
        均值文件 = os.path.join(本目录, "schema_store", "mean.npy")
        if os.path.exists(均值文件) and "schema_store" in 维们:
            均维 = int(_np.load(均值文件, mmap_mode="r").shape[0])
            if 均维 != 维们["schema_store"]:
                问题.append(
                    f"schema_store 的 mean.npy 是 {均维} 维、vectors.npy 是 "
                    f"{维们['schema_store']} 维 —— 中心化均值与库不同源，阈值会失效")
    except Exception as e:                                    # noqa: BLE001
        问题.append(f"向量库自检失败（读不到 npy）：{type(e).__name__}: {e}")

    _配置问题缓存 = 问题
    return list(问题)


@app.get("/health")
async def 健康():
    """轻量健康检查 —— **不发任何外部请求**，前端可放心轮询。"""
    from hybrid_search import 重排状态

    工具们 = [t.name for t in __import__("graph").tools]
    重排 = 重排状态()
    降级 = _工具降级(工具们)
    # 静态配置问题（模型名/库维度）—— 零成本，防"配错了但还没触发"
    降级.extend(_配置自检())

    if 重排.get("末次降级"):
        降级.append(f"精排已降级（本次退回 RRF）：{(重排.get('末次错误') or '')[:140]}")
    elif 重排.get("失败次数"):
        降级.append(f"精排失败过 {重排['失败次数']} 次（当前已恢复，但仍值得看一眼）")

    return {
        "ok": True,
        # ⭐ 新增：一眼判断"是不是在悄悄降级"。true = 所有链路都健康。
        "ok_all": not 降级,
        "degraded": 降级,
        "tools": 工具们,
        "tools_count": len(工具们),
        "rerank": 重排,
        # 说明覆盖面：本端点不联网，只能看"运行期状态 + 静态配置"；
        # 真正的连通性（精排打一次、嵌入算一次、DB ping）在 /health/deep。
        "coverage": "light（未验外部连通性，需深度检查请用 /health/deep）",
    }


@app.get("/health/deep")
async def 深度健康():
    """真实探测：精排打一次、嵌入算一次、两个库读一次、MySQL ping 一次。

    与 /health 的分工：这里**有外部成本**，只给人工排查和上线验收用。
    任何一项失败都写进 `checks`，本端点本身永远返回 200（健康检查不能自己 500）。
    """
    import config

    结果: dict = {}
    ok = True

    # ① 精排：真打一次，看是不是在偷偷降级
    try:
        import hybrid_search as hs
        库 = hs._取库()
        候选 = list(range(min(5, 库.条数())))
        t0 = time.time()
        对 = hs.重排("宝钢股份的营业总收入", 候选, 1)
        耗 = round((time.time() - t0) * 1000)
        好 = bool(对)
        结果["rerank"] = {
            "ok": 好, "ms": 耗, "model": config.RERANK_MODEL,
            "host": _主机(config.RERANK_BASE_URL),
            "error": None if 好 else (hs.重排状态().get("末次错误") or "返回空"),
        }
        ok = ok and 好
    except Exception as e:                                    # noqa: BLE001
        结果["rerank"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = False

    # ② 嵌入：查询维度必须与库维度一致 —— 换模型忘了重建库就是在这里暴露
    try:
        from rag_search import _取库 as _取公告库, _取嵌入器
        维 = len(_取嵌入器().embed_query("测试"))
        库维 = int(_取公告库().向量.shape[1])
        结果["embedding"] = {
            "ok": 维 == 库维, "model": config.EMBED_MODEL,
            "query_dim": 维, "store_dim": 库维,
            "error": None if 维 == 库维 else "维度不匹配 → 语义检索已静默失效，必须重建向量库",
        }
        ok = ok and (维 == 库维)
    except Exception as e:                                    # noqa: BLE001
        结果["embedding"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = False

    # ③ 两个向量库条数（不实调，读缓存）
    try:
        from rag_search import _取库 as _取公告库
        import schema_index
        结果["stores"] = {
            "rag_store": _取公告库().条数(),
            "schema_store": schema_index._取库().条数(),
        }
    except Exception as e:                                    # noqa: BLE001
        结果["stores"] = {"error": f"{type(e).__name__}: {e}"}

    # ④ MySQL：取数类全依赖它，M 盘掉了这里会红
    try:
        import pymysql
        c = pymysql.connect(host=config.DB_HOST, port=config.DB_PORT,
                            user=config.DB_USER, password=config.DB_PASSWORD,
                            database=config.DB_NAME, connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            结果["db"] = {"ok": True, "target": f"{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}"}
        finally:
            c.close()
    except Exception as e:                                    # noqa: BLE001
        结果["db"] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                      "hint": "多半是 M 盘没挂 → M:\\mysql_datadir 不在 → mysqld 起不来"}
        ok = False

    # ⑤ 工具数
    工具们 = [t.name for t in __import__("graph").tools]
    降级 = _工具降级(工具们)
    结果["tools"] = {"count": len(工具们), "expected": 期望工具数, "ok": not 降级, "issues": 降级}
    ok = ok and not 降级

    # ⑥ 静态配置（模型名 / 库维度一致性）
    配置问题 = _配置自检()
    结果["config"] = {"ok": not 配置问题, "issues": 配置问题}
    ok = ok and not 配置问题

    return {"ok": ok, "checks": 结果}


# ══════════════════════════════════════════════════════════════
# ⑦ 前端：从 index.html 文件读取（改前端不用动 server.py）
# ══════════════════════════════════════════════════════════════
def _读前端() -> str:
    with open("index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
async def 首页():
    return _读前端()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
