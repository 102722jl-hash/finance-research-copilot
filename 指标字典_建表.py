# -*- coding: utf-8 -*-
# 指标字典_建表.py —— 把「字段映射.json」+「tools.py 的指标映射」合成一张**指标字典表**
#
# 【为什么建这张表】
#   用户问："我总不能一个新导入的数据就增加一个映射吧？"
#   答案：**不该把映射写死在 Python 里，该放进库、让它可查可检索。**
#   素材已经齐了：
#     · `字段映射.json` = Wind 字段名↔中文名（420 条，覆盖三表 425 列里的 421 列 = 99.1%）
#     · `tools.py::指标映射` = 42 条常用指标（其中 37 条是直取列、5 条是派生算式）
#   本脚本把两者**合成一张表** `workspace.indicator_dict`，并补齐 9 个 json 没覆盖的字段。
#
# 【表设计上的三个关键决定（都是核对报告里发现的坑）】
#   1. **一对多**：中文名会重名（如"其他综合收益"↔ 2 个字段），所以**不做一对一**，
#      而是每行一个 (中文名, 字段)，用 `ambiguous` 标记冲突，查询时能一次返回多个候选。
#   2. **别名**：Wind 中文名带 `加:/减:/其中:` 报表示意（"减:销售费用"），
#      单列 `alias` 存"剥掉前缀"的版本，检索命中率才高。
#   3. **不猜单位**：库里数值量级与公开口径对不上（见 README 局限），
#      所以 `unit` 一律留空并写进 note —— 宁可空着，不要写一个错的口径。
#
# 【幂等】
#   **每次运行全量重建**（先 DELETE 再 INSERT）。要改内容请改 `字段映射.json` 或本脚本，
#   不要直接改表 —— 否则下次重建会被覆盖。
#
# 跑法（需 MySQL 已启动）：
#   "D:/桌面/workspace/study2 - 豆包/langgraph-learning/.venv/Scripts/python.exe" 指标字典_建表.py

import io
import json
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pymysql
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
from tools import 指标映射

表名 = "indicator_dict"
JSON路径 = os.path.join(ROOT, "字段映射.json")

# 段名 → (库表前缀, 真实表名)
段映射 = {
    "利润表": ("ashareincome", "ashareincome_202605261519"),
    "资产负债表": ("asharebalancesheet", "asharebalancesheet_202605261517"),
    "现金流量表": ("asharecashflow", "asharecashflow_202605261518"),
}
# 技术/键字段（不是业务指标）
技术词 = {"object_id", "code", "wind_code", "s_info_windcode", "ann_dt", "report_period",
          "statement_type", "crncy_code", "opdate", "opmode", "comp_type_code",
          "actual_ann_dt", "s_info_compcode"}
# 一句话别名：由「前缀剥离」+「tools.py 里已有的口语名」自动生成（见 3.3）
# ⚠️ 实测踩到：json 用的是 **Wind 报表科目名**（tot_assets = "资产总计"），
#    而用户口语是"总资产"（你原来 `指标映射` 里就这么写的）。
#    如果只信 json 的中文名，**换字典反而会退化** —— 「总资产」查不到。
#    所以必须把 `指标映射` 里的口语名并成别名。
口语别名 = {}
for _名, (_p, _式, _derived) in 指标映射.items():
    if not _derived:
        口语别名.setdefault(_式, set()).add(_名)
# 已知需要人工复核的（抽样发现）
待复核 = {"insur_prem_unearned": "疑似译错：Wind 中「已赚保费」通常为 insur_prem_earned，"
                                 "unearned 更像「未赚/未到期保费」"}
# json 没覆盖的 9 个字段：5 个技术字段按 Wind 常规命名补，4 个业务字段留空待查
补漏 = {
    "opdate": ("操作日期", 1, "不在字段映射.json；按 Wind 常规命名补（技术字段）"),
    "opmode": ("操作模式", 1, "不在字段映射.json；按 Wind 常规命名补（技术字段）"),
    "comp_type_code": ("公司类型代码", 1, "不在字段映射.json；按 Wind 常规命名补（技术字段）"),
    "tot_opt_inc_dif": (None, 0, "字段映射.json 未覆盖：疑似「期权/行权收益调整项」，需查 Wind 手册"),
    "tot_opt_inc_dif_memo": (None, 0, "字段映射.json 未覆盖：同上（memo 备注列）"),
    "tot_opt_cost_dif": (None, 0, "字段映射.json 未覆盖：疑似「期权/行权成本调整项」"),
    "tot_opt_cost_dif_memo": (None, 0, "字段映射.json 未覆盖：同上（memo 备注列）"),
}

# ══════════════════════════════════════════════════════════════
# ① 读素材
# ══════════════════════════════════════════════════════════════
with open(JSON路径, encoding="utf-8") as f:
    映 = json.load(f)
段词条 = {k: v for k, v in 映.items() if isinstance(v, dict)}
来源说明 = 映.get("说明", "")

conn = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                       password=DB_PASSWORD, database=DB_NAME, charset="utf8mb4")
cur = conn.cursor()

# 库里的真实类型（拼进字典，方便判断"能不能算"）+ 真实列集合（判断 json 里的字段是否真存在）
库类型 = {}
库列集 = {}
for s, (_, t) in 段映射.items():
    cur.execute("""SELECT COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""", (DB_NAME, t))
    rows = cur.fetchall()
    库列集[s] = {c for c, _ in rows}
    for c, ty in rows:
        库类型[(s, c)] = ty

# ══════════════════════════════════════════════════════════════
# ② 建表
# ══════════════════════════════════════════════════════════════
# ⚠️ 先 DROP 再 CREATE（不是 IF NOT EXISTS）：本表**每次全量重建**，
#    这样改了上面的 DDL 也能立刻生效，不会因为"表已存在"而悄悄用旧结构。
cur.execute(f"DROP TABLE IF EXISTS {表名}")
cur.execute(f"""
CREATE TABLE {表名} (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  cn_name       VARCHAR(120) NULL     COMMENT '中文名（原样，可能带 加:/减:/其中: 前缀）',
  alias         VARCHAR(400) NULL     COMMENT '别名（**逗号分隔可多个**：前缀剥离名 / 口语名；检索用）',
  prefix        VARCHAR(10)  NULL     COMMENT '报表示意前缀：加:/减:/其中:',
  statement     VARCHAR(20)  NOT NULL COMMENT '利润表/资产负债表/现金流量表/派生',
  table_prefix  VARCHAR(40)  NULL     COMMENT '库表前缀 ashareincome 等',
  field         VARCHAR(80)  NULL     COMMENT '英文字段名（派生指标为 NULL）',
  expr          VARCHAR(400) NULL     COMMENT '派生算式（SQL 片段，直接下沉到库算）',
  is_derived    TINYINT      NOT NULL DEFAULT 0 COMMENT '1=派生指标（不是物理列）',
  is_technical  TINYINT      NOT NULL DEFAULT 0 COMMENT '1=技术/键字段，不是业务指标',
  is_common     TINYINT      NOT NULL DEFAULT 0 COMMENT '1=在 tools.py 常用指标白名单里',
  ambiguous     TINYINT      NOT NULL DEFAULT 0 COMMENT '1=该中文名对应多个字段（一对多）',
  data_type     VARCHAR(30)  NULL     COMMENT '库中真实类型',
  in_db         TINYINT      NOT NULL DEFAULT 1 COMMENT '1=该字段在库里真实存在；0=json 有但库里没有（命名版本差异，别拿它拼 SQL）',
  unit          VARCHAR(20)  NULL     COMMENT '单位（**待确认，故意留空**）',
  reviewed      TINYINT      NOT NULL DEFAULT 0 COMMENT '1=有权威来源/已核对；0=待核',
  source        VARCHAR(40)  NULL     COMMENT '来源：字段映射.json / tools.py / 手工补',
  note          VARCHAR(240) NULL,
  -- ⚠️ 这里**故意不加 UNIQUE**：MySQL 默认排序规则大小写不敏感，
  --    现金流量表里的 `others` 与 `OTHERS` 会被判定成"重复"（实测报 1062）。
  --    它们是两个真实存在的不同列，所以只建普通索引；表的幂等靠"先 DELETE 再 INSERT"。
  KEY idx_stmt_field (statement, field),
  KEY idx_cn (cn_name),
  KEY idx_alias (alias),
  KEY idx_field (field)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='财务指标字典（由 指标字典_建表.py 全量重建）'
""")
conn.commit()

# ══════════════════════════════════════════════════════════════
# ③ 组装行
# ══════════════════════════════════════════════════════════════
行 = []          # (cn_name, alias, prefix, statement, table_prefix, field, expr,
                 #  is_derived, is_technical, is_common, ambiguous, data_type, unit,
                 #  reviewed, source, note)

# 3.1 反查重名（按**不同字段名**去重）
反查 = {}
for s, 词条 in 段词条.items():
    for fld, cn in 词条.items():
        反查.setdefault(cn, set()).add(fld)
重名中文 = {cn for cn, fs in 反查.items() if len(fs) > 1}

# 3.2 常用指标白名单（tools.py）——直取列的那些
常用字段 = set()
for _, (_, 取数式, 派生) in 指标映射.items():
    if not 派生:
        常用字段.add(取数式)

# 3.3 json 的 420 条
for s, 词条 in 段词条.items():
    前缀s, _ = 段映射[s]
    for fld, cn in 词条.items():
        前缀 = ""
        for p in ("加:", "减:", "其中:"):
            if cn.startswith(p):
                前缀 = p
                break
        别名集 = []
        if 前缀:
            别名集.append(cn[len(前缀):])              # 剥掉"加:/减:/其中:"
        别名集 += sorted(口语别名.get(fld, set()) - {cn})   # 并入 tools.py 的口语名
        别名集 = [a for a in dict.fromkeys(别名集) if a]     # 去重 + 保序 + 去空
        别名 = ",".join(别名集) if 别名集 else None
        note = None
        reviewed = 1
        if fld in 待复核:
            reviewed = 0
            note = 待复核[fld]
        if fld in 常用字段:
            note = (note + "；" if note else "") + "已在 tools.py 常用指标白名单"
        在库 = 1 if fld in 库列集[s] else 0
        if not 在库:
            note = (note + "；" if note else "") + \
                "⚠️ 库里没有同名字段（json 的命名版本差异，别拿它拼 SQL）"
        行.append((cn, 别名, 前缀 or None, s, 前缀s, fld, None,
                   0, 1 if fld in 技术词 else 0, 1 if fld in 常用字段 else 0,
                   1 if cn in 重名中文 else 0, 库类型.get((s, fld)), 在库,
                   None, reviewed, "字段映射.json", note))

# 3.4 补漏 9 个（json 未覆盖）
for s, (_, 真实表) in 段映射.items():
    cur.execute("""SELECT COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""", (DB_NAME, 真实表))
    for fld, ty in cur.fetchall():
        if fld in 补漏:
            cn, reviewed, note = 补漏[fld]
            行.append((cn, None, None, s, 段映射[s][0], fld, None,
                       0, 1 if fld in 技术词 else 0, 0, 0, ty, 1,
                       None, reviewed, "手工补", note))

# 3.5 tools.py 里的 5 条派生指标
派生条 = [(k, v) for k, v in 指标映射.items() if v[2]]
for cn, (前缀s, 式, _) in 派生条:
    行.append((cn, None, None, "派生", 前缀s, None, 式,
               1, 0, 1, 0, "double", 1, None, 1, "tools.py",
               "派生指标：算式已下沉到 SQL（除法套 NULLIF 防 0）"))

# ══════════════════════════════════════════════════════════════
# ④ 全量重建 + 插入
# ══════════════════════════════════════════════════════════════
cur.execute(f"DELETE FROM {表名}")
cur.executemany(
    f"""INSERT INTO {表名}
        (cn_name, alias, prefix, statement, table_prefix, field, expr,
         is_derived, is_technical, is_common, ambiguous, data_type, in_db, unit,
         reviewed, source, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", 行)
conn.commit()

# ══════════════════════════════════════════════════════════════
# ⑤ 校验 + 自测
# ══════════════════════════════════════════════════════════════
def 一条(sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()

print("=" * 78)
print(f"✅ 指标字典表已重建：{DB_NAME}.{表名}")
print("=" * 78)
总 = 一条(f"SELECT COUNT(*) FROM {表名}")[0][0]
print(f"  总行数            : {总}")
print(f"  其中 派生指标      : {一条(f'SELECT COUNT(*) FROM {表名} WHERE is_derived=1')[0][0]}")
print(f"  其中 技术/键字段   : {一条(f'SELECT COUNT(*) FROM {表名} WHERE is_technical=1')[0][0]}")
print(f"  其中 常用白名单    : {一条(f'SELECT COUNT(*) FROM {表名} WHERE is_common=1')[0][0]}")
print(f"  待核对(reviewed=0) : {一条(f'SELECT COUNT(*) FROM {表名} WHERE reviewed=0')[0][0]}")
print(f"  重名(ambiguous=1)  : {一条(f'SELECT COUNT(*) FROM {表名} WHERE ambiguous=1')[0][0]}")
print(f"  有别名(含前缀剥离) : {一条(f'SELECT COUNT(*) FROM {表名} WHERE alias IS NOT NULL')[0][0]}")
print(f"  无中文名(cn_name空): {一条(f'SELECT COUNT(*) FROM {表名} WHERE cn_name IS NULL')[0][0]}")
print(f"  库里不存在(in_db=0): {一条(f'SELECT COUNT(*) FROM {表名} WHERE in_db=0')[0][0]}")

print()
print("─" * 78)
print("⑥ 查询自测（这正是「用户问一个词 → 该查什么」要走的路）")
print("─" * 78)
查询 = """
SELECT statement, cn_name, alias, field, is_derived, ambiguous, reviewed, in_db
FROM {t}
WHERE cn_name = %s OR FIND_IN_SET(%s, alias)
ORDER BY is_derived, ambiguous DESC
""".format(t=表名)
for 词 in ["营业总收入", "销售费用", "毛利率", "净利润", "归母净利润",
           "其他综合收益", "以摊余成本计量的金融资产", "拆出资金净增加额"]:
    rs = 一条(查询, (词, 词))
    if not rs:
        print(f"  「{词}」→ ❌ 查不到")
        continue
    # ⚠️ 标签要看**命中条数**，不能只看 ambiguous 标记 ——
    #    因为别名匹配（如"净利润"同时命中别名词）也会产生多候选。
    标记 = "‼️ 多候选" if len(rs) > 1 else "✅ 唯一"
    print(f"  「{词}」→ {标记}（{len(rs)} 条）")
    for stmt, cn, al, fld, der, amb, rev, indb in rs:
        if der:
            kind = "**派生算式**"
        else:
            小旗 = "" if indb else " ⚠️库里无此列"
            kind = f"字段 {fld}{小旗}"
        旗 = "" if rev else "（待核）"
        别名旗 = f"，别名={al}" if al else ""
        print(f"        · [{stmt}] {cn}{别名旗} → {kind}{旗}")

print()
print("─" * 78)
print("⑦ 无中文名的行（下一步要补的就是这些）")
print("─" * 78)
for stmt, fld, note in 一条(f"SELECT statement, field, note FROM {表名} "
                           f"WHERE cn_name IS NULL ORDER BY statement, field"):
    print(f"  [{stmt}] {fld:<26} {note}")

print()
print("─" * 78)
print("⑧ 库里不存在的字段（json 有、库没有 —— 拼 SQL 时要排除）")
print("─" * 78)
for stmt, cn, fld in 一条(f"SELECT statement, cn_name, field FROM {表名} "
                          f"WHERE in_db=0 ORDER BY statement, field"):
    print(f"  [{stmt}] {cn} → {fld}")
print("  说明：这些是 json 的**命名版本差异**（库里叫 s_info_windcode/wind_code，"
      "且没有小写 others）。字典保留它们只为「翻译」，取数时不能用。")

cur.close()
conn.close()
print()
print("=" * 78)
print("下一步可选：① 把「查财务指标」改成查这张表（不再用 Python dict）；")
print("            ② 给 421 条中文名建向量索引 → schema 检索（RAG）。")
print("=" * 78)
