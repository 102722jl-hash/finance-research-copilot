# hybrid_search.py —— v3 第 3 讲：混合检索（向量 + BM25 融合）＋ 交叉编码器重排。
#
# 【三层漏斗（本讲的骨架）】
#   ① 粗筛（recall 优先）：向量 top-30 ∪ BM25 top-30   —— 宁滥勿缺，先别漏掉对的
#   ② 融合（RRF）：把两路排名合成一个排名              —— 不靠调权重，靠"名次"融合
#   ③ 精排（precision 优先）：交叉编码器对 ≤30 条逐条打分 → 取 top-5
#
#   为什么这么设计：向量和 BM25 各有盲区，**并在粗筛阶段互补**；
#   而精排模型（cross-encoder）又贵又慢，**只能上小候选集**，所以放最后。
#   "粗筛宽、精排窄"是所有生产级检索系统的共同形状（搜索、推荐都一样）。
#
# 【RRF 是什么】
#   Reciprocal Rank Fusion：只排名次、不比分数。
#       RRF(d) = Σ_over_各路  1 / (k + rank_路(d))     k 通常取 60
#   好处：向量的余弦（0~1）和 BM25 的分数（0~几十）**量纲完全不同，根本没法加权平均**。
#   RRF 直接绕开"怎么调权重"这个坑 —— 只问"这一路把它排第几"。

import json
import os
import time
import urllib.error
import urllib.request

from bm25 import BM25
from config import RERANK_API_KEY, RERANK_BASE_URL, RERANK_MODEL
from rag_search import _取库, _取嵌入器

粗筛条数 = 30          # 每路粗筛拿多少条
RRF常数 = 60           # RRF 的 k
默认方式 = os.getenv("RETRIEVAL_MODE", "融合重排")   # "向量" | "BM25" | "融合" | "融合重排"
# ⚠️ 用 env 而不是写死：换厂商后若 rerank 接口不兼容（如火山方舟的 rerank 走知识库路由、
#    请求体格式不同），把 .env 里 `RETRIEVAL_MODE=融合` 即可**跳过重排**，其余链路照常。
#    环境变量名用 ASCII（RETRIEVAL_MODE）而不是中文，跨平台更稳。

# ── v5-23 续：rerank 的三个安全阀（【为什么必须有】）────────────
#   原实现只有一句 `urlopen(req, timeout=120)`，**没有任何 try/except** ——
#   rerank 一挂（限流 / 超时 / 换厂商后格式不合），异常会一路抛到工具层，
#   **把整个「查公告」一起带死**。而 MCP 那边是有静默降级的，两处行为不一致。
#   三个阀门：
#     ① RERANK_TIMEOUT：120s 太长，一次卡死就把整个查询拖住 → 默认 8s
#     ② 429 重试一次：免费通道必然限流，直接失败太脆
#     ③ 失败降级：退回 RRF 顺序（少一层精排，但检索还能用），并留痕可查
#   ⚠️ 降级**不是静默**：`重排状态()` 会留下失败次数与末次原因，`自检配置.py` 可查。
RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "8"))
RERANK_MAX_DOCS = int(os.getenv("RERANK_MAX_DOCS", "100"))   # 多数厂商单次上限 100~200

# 只做"可观测"，不参与业务判断
_重排状态 = {"失败次数": 0, "末次错误": "", "末次降级": False}


_bm25缓存: BM25 | None = None


def _取BM25() -> BM25:
    global _bm25缓存
    if _bm25缓存 is None:
        _bm25缓存 = BM25([r["文本"] for r in _取库().记录])
    return _bm25缓存


# ── 第 1 路：向量 ──────────────────────────────────────────
def 向量排名(查询: str, 条数: int = 粗筛条数) -> list[tuple[int, float]]:
    return _取库().检索下标(_取嵌入器().embed_query(查询), 条数=条数)


# ── 第 2 路：BM25 ─────────────────────────────────────────
def BM25排名(查询: str, 条数: int = 粗筛条数) -> list[tuple[int, float]]:
    return _取BM25().检索(查询, 条数=条数)


# ── 融合：RRF ─────────────────────────────────────────────
def RRF(排名表们: list[list[tuple[int, float]]], 常数: int = RRF常数) -> list[tuple[int, float]]:
    """只吃"名次"，产出融合后的 [(下标, 融合分)]。"""
    分: dict[int, float] = {}
    for 表 in 排名表们:
        for 名次, (下标, _原始分) in enumerate(表, start=1):
            分[下标] = 分.get(下标, 0.0) + 1.0 / (常数 + 名次)
    return sorted(分.items(), key=lambda kv: -kv[1])


# ── 精排：bge-reranker（cross-encoder）──────────────────────
# ⚠️ 端点/模型走 config 的 RERANK_*（默认沿用 LLM_*）—— 换厂商时**只改 .env**：
#    硅基流动（OpenAI 兼容，bge-reranker-v2-m3 免费档）：
#      RERANK_BASE_URL=https://api.siliconflow.cn/v1
#      RERANK_MODEL=BAAI/bge-reranker-v2-m3
#    ⚠️ 火山方舟接不了：它的 rerank 在**另一套服务**上
#      （api-knowledgebase.mlp.cn-beijing.volces.com/api/knowledge/service/rerank），
#      要 HMAC-SHA256 AK/SK 签名（API Key 实测 403 check sign error），
#      请求体是 {rerank_model, datas:[{query,content}]}、响应只有 score 数组没有 index。

def _解析分数(响应: dict, 候选下标: list[int]) -> list[tuple[int, float]]:
    """把厂商响应统一成 [(下标, 分数)]。**尽调宽容，但不猜**。

    支持两种形状（实测存在的两家）：
      · OpenAI 兼容（硅基流动等）：{"results": [{"index": 0, "relevance_score": 0.83}, ...]}
      · 火山知识库：{"data": {"scores": [0.17, 0.75, ...]}}  ← **没有 index，只能按位置对齐**
    两种都不满足 → 直接抛 ValueError，让上层走降级（而不是返回一个错的排序）。
    """
    结果 = 响应.get("results")
    if isinstance(结果, list) and 结果:
        对: list[tuple[int, float]] = []
        for x in 结果:
            名次 = x.get("index")
            分 = x.get("relevance_score", x.get("score"))
            if not isinstance(名次, int) or not (0 <= 名次 < len(候选下标)) or 分 is None:
                raise ValueError(f"results 里的条目不可用：{x!r}")
            对.append((候选下标[名次], float(分)))
        return 对

    数据 = 响应.get("data")
    分数组 = 数据.get("scores") if isinstance(数据, dict) else 数据
    if isinstance(分数组, list) and len(分数组) == len(候选下标):
        return [(候选下标[i], float(s)) for i, s in enumerate(分数组)]

    raise ValueError(f"无法识别的 rerank 响应：{str(响应)[:200]}")


def 重排(查询: str, 候选下标: list[int], 条数: int = 5,
         模型: str = RERANK_MODEL) -> list[tuple[int, float]]:
    """把候选原文连同查询一起喂给 cross-encoder，逐条打"相关性分"，再排序。

    和向量的本质区别：向量是"查询和文档**各自**编码再比方向"（bi-encoder，快但粗）；
    重排是"查询和文档**一起**进模型"（cross-encoder，慢但准 —— 能看清词与词的交互）。

    ⚠️ **失败不抛异常，返回 []**：由 `检索下标列表` 降级到 RRF。
       原因见文件头「三个安全阀」——精排是"锦上添花"，不能因为它挂了让检索整个不可用。
    """
    if not 候选下标:
        return []
    候选下标 = 候选下标[:RERANK_MAX_DOCS]
    库 = _取库()
    文档们 = [库.记录[i]["文本"] for i in 候选下标]
    body = {"model": 模型, "query": 查询, "documents": 文档们}
    地址 = RERANK_BASE_URL.rstrip("/") + "/rerank"

    def 发() -> dict:
        req = urllib.request.Request(
            地址,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {RERANK_API_KEY}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        try:
            应答 = 发()
        except urllib.error.HTTPError as e:
            if e.code != 429:                  # 只有限流值得重试；其余（403/404/400）重试也没用
                raise
            time.sleep(0.4)
            应答 = 发()
        对 = _解析分数(应答, 候选下标)
        对.sort(key=lambda kv: -kv[1])
        _重排状态["末次降级"] = False
        return 对[:条数]
    except Exception as e:                      # noqa: BLE001 —— 精排失败一律降级
        _重排状态["失败次数"] += 1
        _重排状态["末次错误"] = f"{type(e).__name__}: {e}"
        _重排状态["末次降级"] = True
        print(f"[重排] 失败 → 本次退回 RRF 顺序（{_重排状态['末次错误'][:160]}）")
        return []


def 重排状态() -> dict:
    """给 `自检配置.py` / 排查用：rerank 是否在悄悄降级。

    ⚠️ 存在的理由：降级是"能跑但更差"，最难发现。没有这个出口，
       就只能靠"感觉结果不准"来猜。
    """
    return dict(_重排状态)


# ── 统一入口：按 方式 组装漏斗 ─────────────────────────────
def 检索下标列表(查询: str, 条数: int = 5, 方式: str = 默认方式) -> list[tuple[int, float]]:
    if 方式 == "向量":
        return 向量排名(查询, 条数)
    if 方式 == "BM25":
        return BM25排名(查询, 条数)
    if 方式 == "融合":
        return RRF([向量排名(查询), BM25排名(查询)])[:条数]
    if 方式 == "融合重排":
        融合表 = RRF([向量排名(查询), BM25排名(查询)])
        候选 = [i for i, _ in 融合表][:粗筛条数]
        排好 = 重排(查询, 候选, 条数)
        # ⭐ 降级：精排没结果就用 RRF 顺序。**分数语义随之从"cross-encoder 分"变成"RRF 分"**
        #    —— 这正是 `检索()` docstring 里说"相似度含义随方式变"的原因，
        #    所以降级时不需要改调用方，但看分数的人必须知道（日志里有 [重排] 失败）。
        return 排好 if 排好 else 融合表[:条数]
    raise ValueError(f"未知方式：{方式}")


def 检索(查询: str, 条数: int = 5, 方式: str = 默认方式) -> list[dict]:
    """返回带出处的记录列表（每条 = 原记录的 股票/文件/页码/文本 + "相似度"字段）。

    ⚠️ "相似度"字段的含义随 方式 变：
      方式="向量"      → 余弦相似度（0~1，通常挤在 0.8x）
      方式="BM25"      → BM25 分（任意实数，量纲不同！）
      方式="融合"      → RRF 融合分
      方式="融合重排"  → **cross-encoder 相关性分**（0~1，区分度大）
    四种分数**不可跨方式比较**。要拼成给模型看的证据块，用 rag_search.拼证据()。

    ⚠️ 方式="融合重排" 且 rerank 调用失败时会**降级**成"融合"（分数字段变 RRF 分），
       不会抛异常、也不会返回空 —— 判断有没有降级用 `重排状态()`。
    """
    库 = _取库()
    命中 = []
    for i, s in 检索下标列表(查询, 条数=条数, 方式=方式):
        一条 = dict(库.记录[i])
        一条["相似度"] = round(s, 4)     # 融合/RRF 时它是融合分，不代表余弦
        命中.append(一条)
    return 命中


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]).strip() or "宝钢股份2023年度的营业总收入"
    for 方式 in ("向量", "BM25", "融合", "融合重排"):
        print(f"\n===== 方式 = {方式} =====")
        for i, h in enumerate(检索(q, 条数=5, 方式=方式), start=1):
            print(f"[{i}] {h['文件']} 第{h['页码']}页  分={h['相似度']}")
