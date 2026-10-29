# state.py —— 图的状态（State）
#
# 这块是「第 2 课 reducer」+「第 5 课自定义 reducer」的落点。
# 我已经把结构写对了，你重点看注释里问的那几句，确认你真懂为什么这么写。

from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages


# ─────────────────────────────────────────────────────────────
# 第 5 课：自定义 reducer。
# evidence 要"按 (来源, 页码) 去重"——否则同一份证据被多个节点追加多次。
# 现在给你一个"先不去重、只合并"的最简版，✍️ 你之后按去重逻辑改它。
# 想想：为什么 evidence 不能直接用 list 当普通字段？（提示：同超步多个节点都想写它）
# ─────────────────────────────────────────────────────────────
def merge_evidence(existing: list, update: list) -> list:
    # ✍️ 动手区：在这里实现去重，比如
    #   seen = {(e["source"], e.get("page")) for e in existing}
    #   return existing + [e for e in update if (e["source"], e.get("page")) not in seen]
    return existing + update


class Evidence(TypedDict):
    source: str        # 来自哪张表 / 哪份年报
    page: str | None   # 年报页码（如果是 PDF 来源）
    value: str         # 具体数值或结论


class AgentState(TypedDict):
    # 第 2 课：messages 用 add_messages 这个内置 reducer —— 节点返回消息时"追加"而不是"覆盖"。
    messages: Annotated[list, add_messages]

    # 第 5 课：evidence 用你上面的自定义 reducer。
    evidence: Annotated[list, merge_evidence]

    # ✍️ 可选：你可以加一个普通字段当"要不要再查一次"的开关。
    #   但 v1 更推荐的做法是直接读 messages[-1].tool_calls（见 graph.py 的 should_continue）。
    #   先留着感受一下"普通字段 vs Annotated 字段"的区别。
    turn_count: int

    # ── v5 第 5 讲新增：三通道分诊的结果 ──
    #    这两个字段**故意不加 reducer**（普通字段，"后写覆盖"）。
    #    因为分诊只在图的最开头跑一次，不存在"多节点同时写"的合并问题 ——
    #    对照 evidence 那种"多节点都想写"的字段，才需要 Annotated + 自定义 reducer。
    #    读取一律用 state.get("通道")，因为老会话（v5-4 之前建的 checkpoint）里没有这个键。
    通道: str          # "A" 结构化查询 / "B" 受约束泛问 / "C" 拒绝通道 / "D" 概念解释
    通道理由: str      # 分诊给出的理由（用于拒答文案与可观测性）
    # v5-16：C 通道内部再分「越界」与「域外」—— 拒答话术不同（不给建议 vs 不属本系统）。
    拒答类别: str      # "C" 越界（默认） / "域外" 与投研无关
    # v5-17：D 通道内部再分「自述」（问本系统能力/工具）—— 提示词不同。
    子类: str          # "" 无 / "自述" / "域外"
