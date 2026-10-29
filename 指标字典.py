# -*- coding: utf-8 -*-
# 指标字典.py —— 查「中文指标名」→ 该用哪张表、哪个字段（或哪条派生算式）
#
# 【它和 tools.py::指标映射 的分工】
#   · `指标映射`（Python dict，42 条）= **人工白名单**：精、快，但只有 42 个，加一个要改代码。
#   · `indicator_dict`（库表，438 行）= **全量字典**：覆盖三表 421 列 + 5 条派生算式，
#     加字段＝改 json 重跑脚本，不用动代码。
#   `tools.py::查财务指标` 的解析顺序是 **先白名单、后字典**（白名单是字典的子集，且人工核过）。
#
# 【为什么返回"候选列表"而不是"一个字段"】
#   核对时发现中文名会**重名**（「其他综合收益」↔ 2 个字段、「净利润」↔ 2 张表），
#   所以接口天生是**一对多**：多个候选时**不要猜**，要么按排序取，要么反问用户。
#   这是**故意**的 —— 硬做一对一会在这些词上悄悄选错。
#
# 【性能：全量载入内存】
#   字典只有 ~435 行，**首次调用时一次读进内存**，之后是纯 Python 匹配（微秒级、零 DB 往返）。
#   ⚠️ 所以本模块被 `tools.py` import 时**没有副作用**（读盘/改 stdout 都放在 `__main__` 里）。
#
# 跑法（自测，需 MySQL 已启动）：
#   "D:/桌面/workspace/study2 - 豆包/langgraph-learning/.venv/Scripts/python.exe" 指标字典.py

import os
import sys

import pymysql

ROOT = os.path.dirname(os.path.abspath(__file__))

表名 = "indicator_dict"
# 排序权：报表有主次（说"净利润"默认指利润表），派生指标单列一档
_表序 = {"利润表": 0, "资产负债表": 1, "现金流量表": 2, "派生": 3}

_缓存 = None


def _全部() -> list[dict]:
    """把字典**一次**读进内存（含 in_db=0 的行，供翻译用；调用方再按需过滤）。

    ⚠️ 读不到就返回空列表并**不抛异常** —— 调用方（`tools.py`）有"工具绝不能抛异常"的硬要求。
    """
    global _缓存
    if _缓存 is not None:
        return _缓存
    try:
        from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
        conn = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                               password=DB_PASSWORD, database=DB_NAME, charset="utf8mb4")
    except Exception:
        _缓存 = []
        return _缓存
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT cn_name, alias, prefix, statement, table_prefix, field, expr,
                       is_derived, is_technical, is_common, ambiguous, data_type,
                       in_db, unit, reviewed, source, note
                FROM {表名}
            """)
            cols = ["cn_name", "alias", "prefix", "statement", "table_prefix", "field",
                    "expr", "is_derived", "is_technical", "is_common", "ambiguous",
                    "data_type", "in_db", "unit", "reviewed", "source", "note"]
            行 = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                # alias 在库里是逗号分隔的多个别名 → 拆成列表，便于匹配
                d["别名表"] = [a for a in (d["alias"] or "").split(",") if a]
                行.append(d)
    except Exception:
        行 = []
    finally:
        conn.close()
    _缓存 = 行
    return _缓存


def _排序键(c: dict):
    return (0 if c["is_derived"] else 1, _表序.get(c["statement"], 9), c["field"] or "")


def 查指标(词: str, 只要可用: bool = True, 含技术字段: bool = False) -> list[dict]:
    """按**中文名或别名**精确查指标，返回**候选列表**（已排序）。

    - `只要可用=True`：过滤 `in_db=0`（json 有、库里没有的命名版本差异；拿它拼 SQL 会报 Unknown column）。
    - `含技术字段=False`：过滤 id/代码/日期这类键字段（不是业务指标）。
    - 排序：派生优先 → 报表主次（利润表 → 资产负债表 → 现金流量表）→ 字段名。
    """
    词 = (词 or "").strip()
    if not 词:
        return []
    出 = [c for c in _全部()
          if (c["cn_name"] == 词 or 词 in c["别名表"])
          and (not 只要可用 or c["in_db"])
          and (含技术字段 or not c["is_technical"])]
    return sorted(出, key=_排序键)


def 搜索(关键词: str, 上限: int = 20) -> list[dict]:
    """**模糊**搜索（子串匹配）—— "检索兜底"的最小版：
    用户说的词不在字典的精确键里时（"毛利润"、"综合收益"）也能找到相关字段。

    ⚠️ 它只是**字符串包含**，不是语义检索。真正的语义版要上向量（下一讲）。
    """
    关键词 = (关键词 or "").strip()
    if not 关键词:
        return []
    出 = [c for c in _全部()
          if c["in_db"] and not c["is_technical"]
          and (关键词 in (c["cn_name"] or "")
               or any(关键词 in a for a in c["别名表"]))]
    return sorted(出, key=_排序键)[:上限]


def 唯一候选(词: str) -> dict | None:
    """**只有一个候选**时返回它；0 个或 ≥2 个都返回 None。

    为什么这样设计：拿到 None 就该**停下来确认**，而不是"随便挑第一个" ——
    「净利润」「其他综合收益」这类词随便挑就是错的。
    """
    候选 = 查指标(词)
    return 候选[0] if len(候选) == 1 else None


def 包含候选(词: str) -> list[dict]:
    """找出**字典里"名字包含这个词"**的条目（`cn_name` 真包含 `词`，且不等同）。

    ⭐ v5-15 新增。它补的是一个**精确查与语义查之间的缝**：
      · 精确查 `查指标("股东权益合计")` → **0 条**（字典里根本没有这个键）；
      · 语义查 → 0.59 直接命中「股东权益合计(含少数股东权益)」，**给了数**；
      → 结果是"数值恰好对（母公司报表含/不含相等），但**标注的名字是错的**"。

      真因：用户说的词是**两个真名的公共前缀**（"股东权益合计(不含…)" / "股东权益合计(含…)"）。
      这种情况**必须反问**，不能靠语义去选一个。

    ⚠️ 用途**只有一个**：`len(包含候选) >= 2` → 反问。
       **绝不用它来自动取数** —— 否则「负债率」会被"名字包含它"的「资产负债率」直接命中，
       把那道"相似度不够就只给建议"的闸门绕开（`负债率` 语义相似度 0.344 < 阈值 0.35，
       现状是**不给数**，这是我们要保住的行为）。
    """
    词 = (词 or "").strip()
    if not 词:
        return []
    出 = [c for c in _全部()
          if c["in_db"] and not c["is_technical"]
          and (c["cn_name"] or "") != 词
          and 词 in (c["cn_name"] or "")]
    return sorted(出, key=_排序键)


def 字典可用() -> bool:
    """字典表是否**真的读到了**（False = 连不上库 / 表不存在 / 查表出错）。

    用途：调用方要能区分两种情况 ——
      · "这指标字典里确实没有"（该如实说"不支持"）；
      · "字典根本没读到"（是环境问题，该说"数据库读不到"，**不该**说"不支持"）。
    不区分的话，MySQL 一挂就会把"库连不上"误报成"暂不支持该指标"，归咎错了对象。
    """
    return bool(_全部())


# ══════════════════════════════════════════════════════════════
# 自测（**放这里而不是模块顶层**：import 本模块不该有任何副作用）
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)

    print("=" * 74)
    print("① 精确查（唯一 vs 多候选）")
    print("=" * 74)
    for 词 in ["营业总收入", "销售费用", "毛利率", "净利润", "总资产", "资产总计",
               "资产减值损失", "其他综合收益", "股息率", "毛利润"]:
        候选 = 查指标(词)
        if not 候选:
            print(f"  「{词}」→ 0 条（可走 搜索() 兜底）")
            continue
        标 = "✅ 唯一" if len(候选) == 1 else f"‼️ {len(候选)} 个候选"
        print(f"  「{词}」→ {标}")
        for c in 候选:
            tgt = ("派生算式 " + (c["expr"] or "")[:40]) if c["is_derived"] \
                else f"{c['table_prefix']}.{c['field']}"
            旗 = "" if c["reviewed"] else "（待核）"
            走过 = "" if c["cn_name"] == 词 else f" ← 别名命中（原:{c['cn_name']}）"
            print(f"        · [{c['statement']}] {tgt}{旗}{走过}")

    print()
    print("=" * 74)
    print("② 唯一候选（None 就必须反问用户，不能瞎猜）")
    print("=" * 74)
    for 词 in ["毛利率", "总资产", "净利润", "其他综合收益", "不存在的词"]:
        r = 唯一候选(词)
        print(f"  「{词}」→ " + (f"{r['statement']}/{r['field'] or '派生'}" if r else "None（需确认）"))

    print()
    print("=" * 74)
    print("③ 模糊兜底（用户说的词不在精确键里）")
    print("=" * 74)
    for 词 in ["毛利", "综合收益", "减值", "现金"]:
        rs = 搜索(词)
        print(f"  「{词}」→ {len(rs)} 条" + (f"：{'、'.join(dict.fromkeys(x['cn_name'] for x in rs))[:80]}" if rs else ""))

    print()
    print("=" * 74)
    print("④ 回归：tools.py 的 42 条常用指标，换字典后是否**一条都没退化**")
    print("=" * 74)
    from tools import 指标映射
    失败 = []
    for 名, (前缀, 取数式, 派生) in 指标映射.items():
        候选 = 查指标(名)
        if 派生:
            好 = any(c["is_derived"] and c["cn_name"] == 名 for c in 候选)
        else:
            好 = any(c["field"] == 取数式 for c in 候选)
        if not 好:
            失败.append((名, 取数式, [(c["cn_name"], c["field"], c["is_derived"]) for c in 候选]))
    print(f"  通过 {len(指标映射) - len(失败)}/{len(指标映射)}")
    for 名, 取数式, 候选 in 失败:
        print(f"  ❌ 「{名}」→ 期望 {取数式}，实际 {候选}")
    if not 失败:
        print("  ✅ 42 条全部命中 —— 说明字典**覆盖了原白名单**，替换不会功能退化")

    print()
    print("=" * 74)
    print("⑤ 长尾抽查：不在原白名单里的字段，现在能不能查到")
    print("=" * 74)
    for 词 in ["资产总计", "营业总成本", "递延所得税资产", "购建固定资产、无形资产和其他长期资产支付的现金"]:
        候选 = 查指标(词)
        if len(候选) == 1:
            c = 候选[0]
            print(f"  「{词}」→ ✅ {c['table_prefix']}.{c['field']}")
        else:
            print(f"  「{词}」→ {len(候选)} 个候选")
