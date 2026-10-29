# -*- coding: utf-8 -*-
# schema_index.py —— v5 第 12 讲：schema RAG（给"指标字典"建向量索引，做语义检索）
#
# 【它解决什么】
#   v5-11 把映射从 Python dict 换成了库表 indicator_dict，覆盖 421 列。
#   但**匹配方式还是字符串**：精确相等（`查指标`）或子串（`搜索`）。
#   用户说"毛利润""一年卖了多少""赚的钱"，都匹配不上 → 这就是"覆盖有了、检索不到"。
#   本讲补上**语义检索**：把每个指标的名字编码成向量，用**意思**找，不用字面对上。
#
# 【为什么不另起一套】
#   复用 v3 已有的两块基础设施（"接口不变、实现可换"的成果）：
#     · `rag_ingest.建embedding()` —— bge-large-zh，1024 维，走同一个网关；
#     · `vector_store.向量库`      —— 本项目自研的 numpy 向量库（40 行，能看清本质）。
#   本文件只负责"把字典变成文档 + 存进去 + 查出来"。
#
# 【嵌入的文本怎么来（重要：不编造）】
#   我们没有每个指标的"释义"，所以**不生成任何定义**，只用字典里已有的信息拼：
#     主名（剥掉"减:/加:/其中:"前缀） + 报表名 + 别名 +（派生指标）机械翻译的算式
#   ⚠️ 派生算式的中文是从字段名反查中文名**机械替换**来的（如 oper_rev→营业收入），
#     不是我编的释义 —— 这样"毛利"才能和"营业收入-营业成本"挂上钩。
#
# 【跑法】
#   建库（需 MySQL + 网关）： ...python schema_index.py
#   检索自测（不重建）    ： ...python schema_index.py --只查
# 库落在 schema_store/（vectors.npy + docs.json）。

import io
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from rag_ingest import 建embedding, 向量化          # 复用 v3 的嵌入器与"批量减半重试"
from vector_store import 向量库

库存目录 = os.path.join(ROOT, "schema_store")

# ── 阈值（**实测调出来的，不是拍的**，见 v5_12_嵌入诊断.py）─────────────
# 背景：直接算余弦，正负例挤在 0.74~0.91，阈值完全没法设（正负都 100% 通过）。
# 对策：**去均值（中心化）** —— 所有向量减去"全体文档平均向量"再归一化，
#   正负例均值差从 0.052 → 0.154（3 倍），AUC 0.852 → 0.889。
# 阈值扫描（`v5_12_语义评测.py`，30 正例 + 9 负例，分数已中心化）：
#   0.25 → 正例 90% 通过 / 负例 44% 误过
#   0.30 → 正例 90% 通过 / 负例 33% 误过
#   0.35 → 正例 73% 通过 / 负例 **0%** 误过（净收益 73%，最高）  ← 采用
#   0.40 → 正例 60% 通过 / 负例 0% 误过（同样零误过，但更保守）
默认阈值 = 0.35    # ≥ 此值：**直接取数**（实测负例零误过）
候选阈值 = 0.30    # ≥ 此值但 < 默认阈值：**只给候选让人确认**，绝不自动取数

_库缓存 = None
_嵌入器缓存 = None
_均值缓存 = None


# ══════════════════════════════════════════════════════════════
# ① 从字典构造"待嵌入文档"
# ══════════════════════════════════════════════════════════════
def _主名(c: dict) -> str:
    """剥掉报表示意前缀（"减:销售费用" → "销售费用"）。"""
    名 = c["cn_name"] or ""
    if c["prefix"] and 名.startswith(c["prefix"]):
        名 = 名[len(c["prefix"]):]
    return 名 or (c["field"] or "")


def _算式转中文(expr: str, 名表: dict) -> str:
    """把派生算式里的英文字段名机械换成中文名（不编造释义）。

    例：CONCAT(ROUND((oper_rev - less_oper_cost)/NULLIF(oper_rev,0)*100,2),'%%')
      → CONCAT(ROUND((营业收入 - 营业成本)/NULLIF(营业收入,0)*100,2),'%%')
    """
    出 = expr or ""
    # 长字段名先替换，避免 tot_oper_rev 被 oper_rev 抢先替换掉
    for fld, cn in sorted(名表.items(), key=lambda kv: -len(kv[0])):
        出 = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(fld)}(?![A-Za-z0-9_])", cn, 出)
    return 出


def 建文档() -> tuple[list[str], list[dict]]:
    """把指标字典里**可用的指标**变成 (待嵌入文本, 记录) 两列。"""
    from 指标字典 import _全部

    全部 = _全部() or []
    名表 = {}
    for c in 全部:
        if c["field"] and not c["is_technical"]:
            名表.setdefault(c["field"], _主名(c))

    文本们, 记录们 = [], []
    for c in 全部:
        if not c["in_db"]:
            continue                                   # json 有、库里没有 → 取数会报错，不进索引
        名 = _主名(c)
        别 = [a for a in c["别名表"] if a and a != 名]
        段 = [f"{c['statement']}", f"{名}"]
        if 别:
            段.append("又称" + "、".join(别))
        if c["is_derived"] and c["expr"]:
            段.append("算式：" + _算式转中文(c["expr"], 名表))
        文本们.append("｜".join(段))
        记录们.append({
            "cn_name": c["cn_name"], "主名": 名, "别名": 别,
            "statement": c["statement"], "table_prefix": c["table_prefix"],
            "field": c["field"], "expr": c["expr"],
            "is_derived": int(c["is_derived"]), "is_technical": int(c["is_technical"]),
            "reviewed": int(c["reviewed"]), "文本": 文本们[-1],
        })
    return 文本们, 记录们


# ══════════════════════════════════════════════════════════════
# ② 建库 / 加载
# ══════════════════════════════════════════════════════════════
def 建库() -> int:
    """全量重建（先删旧文件）。返回入库条数。"""
    文本们, 记录们 = 建文档()
    if not 文本们:
        raise RuntimeError("字典为空（连不上 MySQL 或表没建）→ 先跑 指标字典_建表.py")

    for f in ("vectors.npy", "docs.json"):
        p = os.path.join(库存目录, f)
        if os.path.exists(p):
            os.remove(p)

    print(f"待嵌入 {len(文本们)} 条（来自 workspace.indicator_dict）")
    向量们 = 向量化(建embedding(), 文本们)

    # ⚠️ 关键一步：**去均值（中心化）** —— 直接算余弦时正负例挤在 0.74~0.91 分不开，
    #    减去"全体文档的平均向量"后，不相关的词会被明显压低（实测均值差 0.052→0.154）。
    原 = _归一(np.asarray(向量们, dtype=np.float32))
    均值 = 原.mean(axis=0)
    库向量 = _归一(原 - 均值)

    记录们 = [{k: v for k, v in r.items()} for r in 记录们]
    os.makedirs(库存目录, exist_ok=True)
    np.save(os.path.join(库存目录, "mean.npy"), 均值)   # 查询端要用同一个均值，必须落盘
    向量库(库存目录).写入(库向量.tolist(), 记录们)
    print(f"✅ 已写入 {len(记录们)} 条（已中心化）→ {库存目录}")
    return len(记录们)


def _归一(x: np.ndarray) -> np.ndarray:
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def _取均值() -> np.ndarray:
    """加载建库时的均值向量（查询端必须用**同一个**均值，否则分数不可比）。"""
    global _均值缓存
    if _均值缓存 is None:
        _均值缓存 = np.load(os.path.join(库存目录, "mean.npy")).astype(np.float32)
    return _均值缓存


def _取库():
    global _库缓存
    if _库缓存 is None:
        _库缓存 = 向量库(库存目录).打开()
    return _库缓存


def _取嵌入器():
    global _嵌入器缓存
    if _嵌入器缓存 is None:
        _嵌入器缓存 = 建embedding()
    return _嵌入器缓存


# ══════════════════════════════════════════════════════════════
# ③ 检索入口
# ══════════════════════════════════════════════════════════════
def 语义搜指标(词: str, 条数: int = 5, 含技术字段: bool = False) -> list[dict]:
    """语义检索：按**意思**找指标，不要求字面对上。

    返回 [{主名, statement, field, 相似度, ...}]，按相似度降序。
    ⚠️ 不做阈值截断 —— 阈值交给调用方（不同场景容忍度不同），见 `最像()`。
    """
    词 = (词 or "").strip()
    if not 词:
        return []
    库 = _取库()
    if 库.条数() == 0:
        return []
    # 查询端**必须用建库时同一个均值**去均值，分数才和阈值可比
    原 = np.asarray(_取嵌入器().embed_query(词), dtype=np.float32)
    try:
        q = _归一(原 - _取均值())
    except Exception:
        q = _归一(原)          # 老库没有 mean.npy：退化为不中心化（阈值会失效，重建库可修）
    出 = 库.检索(q.tolist(), 条数=条数 * 4)   # 多取一些，过滤掉技术字段后再截
    if not 含技术字段:
        出 = [r for r in 出 if not r.get("is_technical")]
    return 出[:条数]


def 最像(词: str, 阈值: float = 默认阈值, 条数: int = 5) -> list[dict]:
    """语义检索 + **阈值截断**：只保留相似度 ≥ 阈值的。

    为什么要阈值：向量检索**永远会返回第一名**（哪怕完全不相关）。
    不设阈值 = 把"存货"配给"营收"，比查不到更危险。宁可空，不可错。
    """
    return [r for r in 语义搜指标(词, 条数=条数) if r["相似度"] >= 阈值]


def 语义候选(词: str, 条数: int = 3) -> list[dict]:
    """≥ 候选阈值(0.30) 的候选，**含高分**。调用方按 `相似度` 分档处理：

      - `相似度 ≥ 默认阈值(0.35)` → **直接用**（实测负例零误过）；
      - `候选阈值 ≤ 相似度 < 默认阈值` → **只给候选让人确认**，绝不自动取数。

    ⚠️ 为什么分两档：向量检索**永远会返回第一名**，不设阈值就会把"市盈率"配成某个科目。
    一刀切拒绝又会浪费大量本来能答的口语问法。分档是"既不乱配、又不浪费"的折中。
    """
    return 最像(词, 阈值=候选阈值, 条数=条数)


def 可读(r: dict) -> str:
    return (f"{r['statement']}·{r['主名']}"
            + (f"（{r['field']}）" if r["field"] else "（派生）"))


# ══════════════════════════════════════════════════════════════
# 自测
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    os.chdir(ROOT)

    if "--只查" not in sys.argv:
        建库()
        print()

    用例 = ["毛利率", "毛利润", "一年卖了多少钱", "赚的钱", "总营收",
            "欠别人多少钱", "手上有多少现金", "负债占总资产的比例", "研发花了多少", "折旧摊销",
            # 负例：本库没有，不该被强配
            "市盈率", "股价是多少", "公司有多少员工"]
    print("=" * 92)
    print(f"语义检索自测　高置信阈值 {默认阈值:.2f}｜候选阈值 {候选阈值:.2f}（分数已**中心化**）")
    print("=" * 92)
    for q in 用例:
        rs = 语义候选(q, 条数=3)
        if not rs:
            print(f"  「{q}」→（低于候选阈值，判查不到）")
            continue
        档 = "✅直接用" if rs[0]["相似度"] >= 默认阈值 else "❓仅候选/需确认"
        print(f"  「{q}」→ {档}：" + " ｜ ".join(f"{可读(r)}({r['相似度']:.3f})" for r in rs))
