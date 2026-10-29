# 一键回归.py —— v5 第 14 讲：把 `金标准集.py` 跑成一张表
#
# 【它解决什么问题】
#   项目里已经有 20 个测试脚本，但**每个只覆盖自己那一讲**，改一处不知道跑哪些。
#   本文件只有一个职责：**读金标准集 → 调目标 → 判断言 → 出报告**。
#   测试用例（"应该是什么行为"）全部写在 `金标准集.py`，本文件**不写任何业务期望值**
#   —— 这样"改期望值"只改一个文件，且改的人必须交出"这个值从哪来"（来源字段）。
#
# 【三层设计（关键）】
#   L1 确定性层  —— 不需要 LLM、不联网。纯函数 + 本地库。**秒级**，每次改代码都该跑。
#   L2 外部依赖层 —— 行情 / MCP（起子进程 + 走网络）。**慢**（MCP 启动十几秒），
#                   且**只断言结构、不锚死数值**（股价天天变、LPR 每月变）。
#   L3 端到端层  —— 真的过一遍 LLM 图（`run.py`）。**最慢、要花 token**，默认关，
#                   用 `--含LLM` 打开。它防的是"零件都对、装起来不对"。
#
# 【用法】
#   python 一键回归.py              # L1 + L2
#   python 一键回归.py --只L1        # 只跑确定性层（最快，改代码后先跑这个）
#   python 一键回归.py --含LLM       # 追加 L3 端到端
#   结果同时打印到屏幕，并写 `回归报告.md`。
#
# 【退出码】
#   0 = 所有 P0 全过（KNOWN 红了不影响退出码，只提醒）
#   1 = 有 P0 红了 → 可以直接接进 CI / pre-commit

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
本目录 = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


# ══════════════════════════════════════════════════════════════
# ① 加载模块（按需、惰性）—— 外部模块（realtime / mcp）不进 L1
# ══════════════════════════════════════════════════════════════
def _加载(文件名, 模块名):
    spec = importlib.util.spec_from_file_location(模块名, os.path.join(本目录, 文件名))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _试加载(文件名, 模块名):
    try:
        return _加载(文件名, 模块名)
    except Exception as e:
        print(f"⚠️ 模块 `{文件名}` 加载失败：{type(e).__name__}: {e}", file=sys.stderr)
        return None


_缓存: dict = {}


def 取模块(名):
    if 名 not in _缓存:
        _缓存[名] = _加载(名 + ".py", 名)
    return _缓存[名]


# ══════════════════════════════════════════════════════════════
# ② 目标注册表 —— 用例的 `目标` 字段 → 真正调哪个函数
#     每个 wrapper 接收 `参数: dict`，返回 **str 或 dict 或 list**
# ══════════════════════════════════════════════════════════════
def _调查财务指标(p):
    t = 取模块("tools")
    参 = dict(p)
    参.setdefault("年份", 0)
    参.setdefault("季度", 0)
    return t.查财务指标.invoke(参)


def _调算派生(p):
    d = 取模块("派生指标")
    return d.算派生指标.invoke(dict(p))


def _调行业聚合(p):
    h = 取模块("行业聚合")
    return h.行业聚合.invoke(dict(p))


def _调实时行情(p):
    rt = 取模块("realtime_tool")
    return rt.查实时行情.invoke(dict(p))


def _调分诊(p):
    g = 取模块("guard")
    return g.分诊(p["问题"], 允许LLM=bool(p.get("允许LLM", False)))


def _调检测越界(p):
    g = 取模块("guard")
    # ⭐ 2026-10-20：支持传 `原文` —— 机构观点类（研报/快讯）答案里的「目标价」是引用，不是主张。
    return g.检测越界(p["回答"], p.get("原文", ""))


def _调检查断言(p):
    g = 取模块("guard")
    return g.检查断言(p["回答"], p["支撑文本"])


def _调评估回答(p):
    g = 取模块("guard")
    return g.评估回答(p["回答"], p["工具原文"], p.get("问题原文", ""))


def _调校验可加权(p):
    """v5-16：`可加权比值`（分子/分母）与 指标映射 的 expr 是否**同一定义**。空列表 = 一致。"""
    t = 取模块("tools")
    return t.校验可加权()


def _调关系判据(p):
    """v5-21：蜕变关系判据（纯函数，不联网不调 LLM）。

    ⚠️ 用例的 `参数` 里第一项是**判据名**（同源同组/口径唯一/分项不超合计/时点不倒退/同义一致），
       其余是它要的关键字参数。这里拆开再转发，别把 `判据` 本身当参数传下去。
    """
    m = 取模块("关系判据")
    参 = dict(p)
    名 = 参.pop("判据")
    return m.判(名, **参)


def _调溯源(p):
    """v5-21：直接调 `graph.溯源` 节点（护栏第 4 层）—— 验"硬拦截真的接上了"。

    ⚠️ 这测的不是判据，是**接线**：判据对、但节点里没调用，产品面照样放行。
       所以必须有一条用例走到"拒绝文案"，否则等于没接。
    构造最小 state：最后一条必须是 AIMessage（溯源把它当回答），
    中间的 ToolMessage 提供支撑池。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    g = 取模块("graph")
    消息 = [HumanMessage(content=p.get("问题", ""))]
    if p.get("工具原文"):
        消息.append(ToolMessage(content=p["工具原文"], tool_call_id="t-1"))
    消息.append(AIMessage(content=p["回答"]))
    return g.溯源({"messages": 消息, "通道": p.get("通道", "A")})


def _调MCP(工具名, p):
    br = 取模块("mcp_bridge")
    按名 = {t.name: t for t in br.同步拿工具()}
    if 工具名 not in 按名:
        raise RuntimeError(f"MCP 工具 `{工具名}` 不在已发现列表里（可用：{sorted(按名)}）")
    return 按名[工具名].invoke(dict(p))


目标表 = {
    "查财务指标": _调查财务指标,
    "算派生指标": _调算派生,
    "行业聚合": _调行业聚合,
    "realtime_tool.查实时行情": _调实时行情,
    "guard.分诊": _调分诊,
    "guard.检测越界": _调检测越界,
    "guard.检查断言": _调检查断言,
    "guard.评估回答": _调评估回答,
    "tools.校验可加权": _调校验可加权,
    "关系判据.判": _调关系判据,
    "graph.溯源": _调溯源,
}


def 调目标(目标: str, 参数: dict):
    if 目标.startswith("mcp."):
        return _调MCP(目标[len("mcp."):], 参数)
    if 目标 not in 目标表:
        raise RuntimeError(f"未知目标 `{目标}`（没在 目标表 里注册）")
    return 目标表[目标](参数)


# ══════════════════════════════════════════════════════════════
# ③ 断言引擎 —— 用例的 `断言` 字段 → 真/假
#     操作符（都是可自动判定的，绝不含"看着差不多"）：
#       ("含",    s)          结果文本里包含 s
#       ("不含",  s)          结果文本里**不**包含 s
#       ("取",    k, v)       结果（dict）的 k 字段 == v
#       ("取非空", k)         结果的 k 字段非空
#       ("为空",)             结果为空（[] / {} / 空串）
#       ("非空",)             结果非空
#       ("含元素", s)         结果（list）里包含 s
#       ("正则",  pat)        结果文本匹配正则 pat
#       ("自定义", 名, 参)    调用下面的 自定义断言表[名]（复杂的"关系"断言）
# ══════════════════════════════════════════════════════════════
def _PB关系(结果文本: str, 参数) -> tuple[bool, str]:
    """PB 的期望值不能锚死（分子是**实时股价**）。
    改为断言**公式关系成立**：从输出里读回 股价 与 每股净资产，自己除一遍核对 PB。"""
    s = str(结果文本)
    m价 = re.search(r"最新股价\s*=\s*([\d.]+)", s)
    m净 = re.search(r"每股净资产\s*=\s*归母权益\s*÷\s*期末总股本\s*=\s*[\d,\.]+\s*÷\s*[\d,\.]+\s*=\s*([\d.]+)", s)
    mPB = re.search(r"PB（市净率）\s*=\s*([\d.]+)", s)
    if not (m价 and m净 and mPB):
        return False, f"读不回因子（价={bool(m价)} 净={bool(m净)} PB={bool(mPB)}）"
    价, 净, pb = float(m价.group(1)), float(m净.group(1)), float(mPB.group(1))
    if 净 <= 0:
        return False, f"每股净资产={净} 非正"
    期望 = 价 / 净
    ok = abs(期望 - pb) <= max(0.02, 期望 * 0.01)
    return ok, f"{价}/{净}={期望:.4f} vs 输出 {pb}"


自定义断言表 = {"PB关系": _PB关系}


def 查断言(结果, 断言列表) -> tuple[bool, list[str]]:
    文本 = 结果 if isinstance(结果, str) else str(结果)
    证据 = []
    全过 = True
    for 条 in 断言列表:
        op = 条[0]
        try:
            if op == "含":
                ok = 条[1] in 文本
            elif op == "不含":
                ok = 条[1] not in 文本
            elif op == "取":
                ok = isinstance(结果, dict) and 结果.get(条[1], "__缺__") == 条[2]
            elif op == "取非空":
                ok = isinstance(结果, dict) and bool(结果.get(条[1]))
            elif op == "为空":
                ok = (not 结果) if isinstance(结果, (list, dict, str)) else False
            elif op == "非空":
                ok = bool(结果)
            elif op == "含元素":
                ok = isinstance(结果, (list, tuple, set)) and 条[1] in 结果
            elif op == "正则":
                ok = re.search(条[1], 文本) is not None
            elif op == "自定义":
                fn = 自定义断言表.get(条[1])
                ok, note = (False, f"没有这个自定义断言 {条[1]}") if not fn else fn(结果, 条[2] if len(条) > 2 else None)
                证据.append(f"自定义:{条[1]}→{note}")
                if not ok:
                    证据.append(f"❌{op}")
                else:
                    证据.append(f"✅{op}:{条[1]}")
                全过 &= ok
                continue
            else:
                ok, 证据 = False, 证据 + [f"未知操作符 {op}"]
                全过 = False
                continue
        except Exception as e:
            ok = False
            证据.append(f"断言 {条} 出错: {type(e).__name__}: {e}")
        证据.append(("✅" if ok else "❌") + f"{op}" + (f":{条[1]}" if len(条) > 1 and op != "取" else ""))
        if not ok:
            全过 = False
    return 全过, 证据


# ══════════════════════════════════════════════════════════════
# ④ 单条执行
# ══════════════════════════════════════════════════════════════
class 结果:
    def __init__(self, 用例):
        self.用例 = 用例
        self.通过 = False
        self.耗时 = 0.0
        self.证据 = []
        self.异常 = None
        self.输出 = None


def 跑一条(c) -> 结果:
    r = 结果(c)
    t0 = time.time()
    try:
        out = 调目标(c.目标, c.参数)
        r.输出 = out
        r.通过, r.证据 = 查断言(out, c.断言)
    except Exception as e:
        r.通过 = False
        r.异常 = f"{type(e).__name__}: {e}"
        r.证据 = [f"❌ 调用抛异常：{r.异常}"]
    r.耗时 = time.time() - t0
    return r


# ══════════════════════════════════════════════════════════════
# ⑤ L3 端到端 —— 真的跑一遍 `run.py`（含 LLM）
#     它防的不是"零件"，是"装起来"。只挑**少数几条**最有代表性的。
# ══════════════════════════════════════════════════════════════
L3用例 = [
    ("取数端到端", "宝钢股份2025年营业总收入是多少", [("含", "100688078026.64")]),
    ("派生端到端", "宝钢股份的ROE是多少", [("含", "4.43%")]),
    ("行业端到端", "钢铁行业2025年营业总收入合计是多少", [("含", "9,279.71")]),
    ("口径端到端", "贵州茅台2024年公开财报口径的营业总收入是多少", [("含", "1,741.44")]),
    ("拒答端到端", "推荐一只股票给我", [("含", "无法回答")]),
    ("实时端到端", "宝钢股份现在股价多少", [("含", "数据源")]),
]


def 跑L3():
    出 = []
    for 名, 问题, 断言 in L3用例:
        r = 结果(type("C", (), {"组": "端到端", "名": 名, "目标": "run.py", "级别": "KNOWN",
                                 "来源": "[标注]", "说明": 问题})())
        t0 = time.time()
        try:
            环境 = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            cp = subprocess.run([PYTHON, "run.py", 问题], cwd=本目录, env=环境,
                                capture_output=True, text=True, encoding="utf-8", timeout=300)
            文本 = (cp.stdout or "") + "\n" + (cp.stderr or "")
            r.输出 = 文本
            r.通过, r.证据 = 查断言(文本, 断言)
        except Exception as e:
            r.异常 = f"{type(e).__name__}: {e}"
            r.证据 = [f"❌ {r.异常}"]
        r.耗时 = time.time() - t0
        出.append(r)
    return 出


# ══════════════════════════════════════════════════════════════
# ⑥ 报告
# ══════════════════════════════════════════════════════════════
def 打印层(层名, 结果们):
    print("\n" + "═" * 92)
    print(f"  {层名}")
    print("═" * 92)
    print(f"  {'组':<5}{'用例':<40}{'级别':<7}{'判定':<7}{'耗时':>8}  证据")
    print("  " + "─" * 88)
    for r in 结果们:
        c = r.用例
        if r.通过:
            标 = "✅"
        elif c.级别 == "KNOWN":
            标 = "⚠️已知"
        else:
            标 = "❌"
        证据 = "；".join(x for x in r.证据 if x.startswith(("❌", "自定义"))) or "；".join(r.证据[:3])
        print(f"  {c.组:<5}{c.名:<40}{c.级别:<7}{标:<7}{r.耗时:>7.2f}s  {证据[:60]}")
    统计(结果们)
    return 结果们


def 统计(结果们, 静默=False):
    c = Counter(r.用例.组 for r in 结果们)
    通过 = sum(1 for r in 结果们 if r.通过)
    p0红 = [r for r in 结果们 if not r.通过 and r.用例.级别 == "P0"]
    known红 = [r for r in 结果们 if not r.通过 and r.用例.级别 == "KNOWN"]
    if not 静默:
        print("  " + "─" * 88)
        print(f"  合计 {len(结果们)} 条：通过 {通过} ｜ P0 红 {len(p0红)} ｜ KNOWN 红 {len(known红)}")
        print("  分组：" + " ｜ ".join(f"{k} {v}" for k, v in sorted(c.items())))
    return len(p0红), len(known红)


def 写报告(各层, 路径):
    行 = ["# 回归报告（一键回归.py 自动生成）", "",
           f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
           "- 用例来源：`金标准集.py`（期望值来自独立路径：[SQL]/[复算]/[标注]/[外部]）",
           "- 说明：**P0 红 = 真回归**；KNOWN 红 = 已知偏差/边界，复核后可改。", ""]
    总P0 = 总KN = 0
    for 层名, 结果们 in 各层:
        通过 = sum(1 for r in 结果们 if r.通过)
        行 += [f"## {层名}（{通过}/{len(结果们)} 通过）", "",
               "| 组 | 用例 | 目标 | 级别 | 判定 | 耗时 | 证据 |",
               "|---|---|---|---|---|---:|---|"]
        for r in 结果们:
            c = r.用例
            标 = "✅ 通过" if r.通过 else ("⚠️ 已知偏差" if c.级别 == "KNOWN" else "❌ **红了**")
            证据 = "；".join(r.证据).replace("|", "/")
            行.append(f"| {c.组} | {c.名} | `{c.目标}` | {c.级别} | {标} | {r.耗时:.2f}s | {证据[:120]} |")
        p0, kn = 统计(结果们, 静默=True)
        总P0 += p0
        总KN += kn
        行.append("")
    # KNOWN 明细
    known红 = [r for _, 结果们 in 各层 for r in 结果们 if not r.通过 and r.用例.级别 == "KNOWN"]
    if known红:
        行 += ["## ⚠️ 已知偏差明细（红是「提醒」，不是「新问题」）", ""]
        for r in known红:
            c = r.用例
            行 += [f"### {c.组} · {c.名}",
                   f"- 期望（理想）：{' ｜ '.join(断言可读(x) for x in c.断言)}",
                   f"- 实测：{str(r.输出)[:200]}", f"- 说明：{c.说明}（来源 {c.来源}）", ""]
    行 += ["---", f"**总计：P0 红 {总P0} 条 ｜ KNOWN 红 {总KN} 条**", ""]
    with open(路径, "w", encoding="utf-8") as f:
        f.write("\n".join(行))


def 断言可读(条) -> str:
    op = 条[0]
    m = {"含": "含", "不含": "不含", "取": "取", "取非空": "取非空",
         "为空": "为空", "非空": "非空", "含元素": "含元素", "正则": "正则", "自定义": "自定义"}
    if op == "取":
        return f"取 {条[1]} = {条[2]}"
    if op in ("取非空", "含", "不含", "含元素", "正则"):
        return f"{m[op]} 「{条[1]}」"
    return m.get(op, op)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--只L1", action="store_true", help="只跑确定性层（不联网、不起 MCP）")
    ap.add_argument("--含LLM", action="store_true", help="追加 L3 端到端（花 token，慢）")
    ap.add_argument("--报告", default=os.path.join(本目录, "docs", "报告", "回归报告.md"))
    a = ap.parse_args()

    j = _加载("金标准集.py", "金标准集")
    用例表 = j.用例表
    print(f"📋 载入金标准集：{len(用例表)} 条用例")
    print("  " + " ｜ ".join(f"{k} {v}" for k, v in sorted(Counter(c.组 for c in 用例表).items())))

    各层 = []

    # ── L1 确定性 ──
    L1 = [c for c in 用例表 if c.组 != "外部"]
    print(f"\n▶ L1 确定性层：{len(L1)} 条（不联网 / 无 LLM）")
    各层.append(("L1 确定性层", 打印层("L1 确定性层（无 LLM / 不联网）", [跑一条(c) for c in L1])))

    # ── L2 外部依赖 ──
    if not a.只L1:
        L2 = [c for c in 用例表 if c.组 == "外部"]
        print(f"\n▶ L2 外部依赖层：{len(L2)} 条（行情 + MCP，起子进程较慢）")
        各层.append(("L2 外部依赖层", 打印层("L2 外部依赖层（行情 / MCP）", [跑一条(c) for c in L2])))

    # ── L3 端到端 ──
    if a.含LLM:
        print(f"\n▶ L3 端到端层：{len(L3用例)} 条（真的过 LLM 图，慢）")
        各层.append(("L3 端到端层", 打印层("L3 端到端层（LLM）", 跑L3())))

    写报告(各层, os.path.join(本目录, a.报告))
    print(f"\n📝 报告已写：{a.报告}")

    总P0 = sum(sum(1 for r in rs if not r.通过 and r.用例.级别 == "P0") for _, rs in 各层)
    print("=" * 92)
    print("✅ 全部 P0 通过" if 总P0 == 0 else f"❌ 有 {总P0} 条 P0 红了 —— 这就是真回归，去查上面")
    print("=" * 92)
    return 1 if 总P0 else 0


if __name__ == "__main__":
    sys.exit(main())
