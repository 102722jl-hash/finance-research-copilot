# macro_mcp_server.py —— v5 第 13 讲任务 3：把 **宏观数据** 包成 MCP server（AkShare 免费接口）
#
# 【和 search_mcp_server.py 一样的原则】
#   独立进程 + 独立依赖（akshare），挂了不影响别人；不 import 项目内任何模块。
#
# ⚠️⚠️ 【stdio 传输的第一天坑】⚠️⚠️
#   stdout 是 JSON-RPC 协议通道 → **本文件绝不 print()**。
#   更要命的是 **akshare 自己会往 stdout 打 tqdm 进度条**（实测 `macro_china_lpr()` 就会），
#   那会直接冲乱协议。→ 所以下面每个取数都套了 `redirect_stdout(sys.stderr)`。
#
# 🔴 【实测选源结论（别凭直觉换接口）】
#   · LPR 用 `macro_china_lpr()`：最新 2026-09-20，1Y=3.0 / 5Y=3.5，数据是最新的。
#   · CPI **不能用** `macro_china_cpi_monthly()`：那个是金十口径，
#     **已停更**（最后一个非空"今值"停在 2025-08，比今天滞后 13 个月）。
#     用 **`macro_china_cpi()`**（国家统计局口径，月度），最新 2026-09，含同比/环比。
#     → 教训：**同一个指标有两个接口时，要实测"最新一条是哪天"，不能看名字就想当然。**
#   · 两张表的**排序方向相反**：LPR 是日期升序（最新在末行），CPI 是日期降序（最新在首行）。
#     → 取"最新"前必须显式排序，别用 `tail(1)` 一刀切。

import os
import sys
import time
from contextlib import redirect_stdout

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("宏观数据")


def _静默取(函数):
    """调 akshare，同时把它的 tqdm 等 stdout 输出赶到 stderr（保住协议通道）。"""
    with redirect_stdout(sys.stderr):
        return 函数()


def _日志(话: str) -> None:
    """计时/调试日志 —— **只能走 stderr**（stdout 是 JSON-RPC 协议通道）。"""
    print(f"[外部数据] {话}", file=sys.stderr, flush=True)


# 🔴 【实测踩坑：akshare 必须在**启动时**导入，不能等到第一次调用】
#   懒加载（写在工具函数里 import）时，MCP 子进程里实测要 **46 秒**，
#   而同一个解释器的普通子进程只要 1.2 秒；隔离「管道 stdio」「工作线程」「冷启动」
#   三个变量都复现不出来（详见 v5-第13讲 讲义）。
#   → 不追根因，改结构：**启动时一次性付掉**，之后每次调用都是毫秒级。
#   → 工程原则：**外部依赖的冷启动成本要放在启动期，不要转嫁给第一次用户请求**。
_日志("正在 import akshare（启动时一次性加载）…")
_t0 = time.time()
try:
    import akshare as ak
    _AK = True
except Exception as e:                       # 装不上也得能起，工具会如实报不可用
    ak = None
    _AK = False
    _日志(f"⚠️ import akshare 失败：{type(e).__name__}: {e}")
_日志(f"import akshare 完成，用时 {time.time()-_t0:.1f}s（可用={_AK}）")


@mcp.tool(name="query_lpr")
def 查询LPR() -> str:
    """查中国 **LPR（贷款市场报价利率）** 的最新值。

    什么时候该用：问"LPR 多少""降息了吗""贷款利率""5 年期 LPR"。
    什么时候不该用：问公司财务、股价、公告（那些有别的工具）。

    返回最新一期 **1 年期** 与 **5 年期以上** LPR，以及公布日期。
    ⚠️ LPR 每月 20 日公布，返回的是**已公布的最新一期**，不是预测值。
    """
    if not _AK:
        return "【LPR 查询失败】akshare 未安装或导入失败，请 `pip install akshare` 后重启 MCP server"
    _日志("query_lpr 开始")
    t0 = time.time()
    try:
        df = _静默取(ak.macro_china_lpr)
        _日志(f"  macro_china_lpr 用时 {time.time()-t0:.1f}s")
    except Exception as e:
        return f"【LPR 查询失败】{type(e).__name__}: {e}（akshare 接口出错，请稍后重试）"

    if df is None or df.empty:
        return "【LPR】没有取到数据"

    # LPR 表是**日期升序** → 最新在最后一行；仍显式排序，别依赖"碰巧"
    d = df.dropna(subset=["LPR1Y"]).sort_values("TRADE_DATE")
    if d.empty:
        return "【LPR】取到了表，但没有有效的 LPR1Y 值"

    r = d.iloc[-1]
    return (f"【LPR 贷款市场报价利率】公布日期 {r['TRADE_DATE']}\n"
            f"  · 1 年期 LPR = {r['LPR1Y']}%\n"
            f"  · 5 年期以上 LPR = {r['LPR5Y']}%\n"
            f"  ·（同一表内的贷款基准利率 RATE_1={r.get('RATE_1')} / RATE_2={r.get('RATE_2')}）\n"
            f"数据源：AkShare（东方财富）｜每月 20 日公布，此为已公布的最新一期")


@mcp.tool(name="query_cpi")
def 查询CPI() -> str:
    """查中国 **CPI（居民消费价格指数）** 的最新数据。

    什么时候该用：问"CPI 多少""通胀怎么样""物价涨幅""通缩了吗"。
    什么时候不该用：问 PPI、GDP、利率（本工具只有 CPI）。

    返回最新月份：全国 CPI 当月指数、同比涨幅、环比涨幅、累计指数。
    ⚠️ 用的是**国家统计局**口径（"全国-同比增长"就是常说的"CPI 同比"）；
       当月指数 100.9 表示"比去年同期涨 0.9%"，别把指数值当涨幅念。
    """
    if not _AK:
        return "【CPI 查询失败】akshare 未安装或导入失败，请 `pip install akshare` 后重启 MCP server"
    _日志("query_cpi 开始")
    t0 = time.time()
    try:
        df = _静默取(ak.macro_china_cpi)
        _日志(f"  macro_china_cpi 用时 {time.time()-t0:.1f}s")
    except Exception as e:
        return f"【CPI 查询失败】{type(e).__name__}: {e}（akshare 接口出错，请稍后重试）"

    if df is None or df.empty:
        return "【CPI】没有取到数据"

    # CPI 表是**日期降序** → 最新在第一行；仍按"月份"字符串显式排序（YYYY年MM月份，字典序=时间序）
    d = df.sort_values("月份", ascending=False)
    r = d.iloc[0]
    return (f"【CPI 居民消费价格指数】{r['月份']}（国家统计局口径）\n"
            f"  · 全国 CPI 当月指数 = {r['全国-当月']}（上年同月=100）\n"
            f"  · 同比涨幅 = {r['全国-同比增长']}%\n"
            f"  · 环比涨幅 = {r['全国-环比增长']}%\n"
            f"  · 累计指数 = {r['全国-累计']}\n"
            f"  · 城市同比 {r.get('城市-同比增长')}% ｜ 农村同比 {r.get('农村-同比增长')}%\n"
            f"数据源：AkShare（国家统计局）｜⚠️ 月度数据，公布有滞后，引用时务必带上月份")


# ══════════════════════════════════════════════════════════════
# v5 第 15 讲：公开（**合并报表**）口径的财报数据
# ══════════════════════════════════════════════════════════════
# 【为什么需要它 —— 这是本项目最"值钱"的一个补丁】
#   本地 workspace 库里的三大报表是 **母公司（个别）报表**（statement_type 只有 408006000），
#   所以"营收/总资产/权益"这些**绝对量**和公开（合并报表）数**对不上**
#   （茅台 2024 营业总收入：库 945 亿 vs 公开 1741 亿 = 0.54 倍；万科甚至 0.01 倍）。
#   之前只能一遍遍解释"口径不同、不可比"——用户听着像借口。
#   现在：**同一只股票、同一个报告期，两头都能给数**，"口径差"从"解释"变成"可核对"。
#
# 【接口选择（实测）】
#   用 `ak.stock_financial_report_sina(stock='sh600019', symbol='利润表')`
#   → 返回 105 期 × 83 列，**列名就是中文科目名**（"营业总收入"/"归属于母公司所有者的净利润"…），
#     单位是元，`报告日` 形如 20251231。三张表分别取 symbol=利润表 / 资产负债表 / 现金流量表。
#   → 比 `stock_financial_abstract` 细得多（那个只有摘要口径）。
#
# 【为什么不用项目里的名字映射】
#   MCP server 是**独立进程、零项目依赖**（v5-13 定的原则）→ 不能 import tools.py。
#   所以这里靠"列名直接匹配 + 包含匹配"来认科目，并把"我匹配到了哪一列"写进返回体。

_名码缓存: dict | None = None
_报表缓存: dict = {}          # (代码, 表名) → (DataFrame, 时间戳)；TTL 300s

# 项目里已有的**全市场名→码表**（v5-8 产出，5929 条）。
# ⚠️ 这不算"依赖项目模块"：它只是一份**数据文件**（json），不进 import 图、不带依赖。
#    优先用它，是因为**本机有 HTTP 代理，只放行白名单域名** ——
#    akshare 的 `stock_zh_a_spot_em()` 打的是 `82.push2.eastmoney.com`，**被代理挡掉**
#    （实测 ProxyError）。本地表既快又不受网络影响。
_名码文件 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_names.json")
# ⭐ 别名表：**集团名 / 品牌名 → 证券简称/代码**。
#   为什么必须有：交易所简称和公司对外的名字经常不一致，用户问的是后者。
#   实测案例（2026-10-19）：用户问「中亿丰最新财报」，本表里查不到「中亿丰」——
#     因为 002333 的**证券简称至今仍是「罗普斯金」**（新浪行情接口实测返回也是「罗普斯金」），
#     而「中亿丰」是它的控股集团品牌名。另一家 AI 能答出来，靠的正是这层别名关系。
#   ⇒ 名表再新也没用（源里就叫旧名），**唯一解是维护一张别名表**。
_别名文件 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_aliases.json")


def _本地名码() -> dict:
    try:
        import json
        with open(_名码文件, encoding="utf-8") as f:
            return json.load(f)          # {"贵州茅台": "600519.SH", ...}
    except Exception as e:
        _日志(f"⚠️ 本地名码表读不到（{type(e).__name__}: {e}），将尝试联网兜底")
        return {}


def _本地别名() -> dict:
    """别名表：集团名/品牌名 → 证券代码。文件不存在就当空表（不影响主流程）。"""
    try:
        import json
        with open(_别名文件, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except Exception:
        return {}


def _代码化(股票: str) -> str | None:
    """把 '600019.SH' / 'sh600019' / '宝钢股份' 统一成 sina 要的 'sh600019'。"""
    s = (股票 or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith(("sh", "sz", "bj")) and low[2:].isdigit():
        return low
    if "." in s:                                   # 600019.SH
        号, _, 所 = s.partition(".")
        所 = 所.upper()
        if 号.isdigit() and 所 in ("SH", "SZ", "BJ"):
            return 所.lower() + 号
    if s.isdigit() and len(s) == 6:                # 裸代码 → 推交易所
        前 = s[0]
        所 = "sh" if 前 == "6" else ("sz" if 前 in "03" else "bj")
        return 所 + s
    return None


def _名转码(名: str) -> str | None:
    """公司名 → wind 风格代码。**别名表 → 本地名表 → 前缀匹配**。

    ⚠️ 别名表必须排第一：用户说的常是**集团名/品牌名**，而证券简称不一定是它
       （实测「中亿丰」→ 002333「罗普斯金」，名表里永远查不到，因为源里就登记旧名）。
    """
    global _名码缓存
    名 = (名 or "").strip()
    if not 名:
        return None
    码 = _本地别名().get(名)
    if 码:
        return 码
    if _名码缓存 is None:
        _名码缓存 = _本地名码()
        if not _名码缓存:
            try:
                df = _静默取(ak.stock_zh_a_spot_em)   # 联网兜底（可能被代理挡）
                _名码缓存 = {str(r["名称"]).strip(): str(r["代码"]).strip()
                            for _, r in df.iterrows()}
            except Exception as e:
                _日志(f"⚠️ 全市场名单拉取也失败：{type(e).__name__}: {e}")
                _名码缓存 = {}
    if 名 in _名码缓存:
        return _名码缓存[名]
    # 去掉 *ST / 空格之类再试；再不行做前缀匹配（"宝钢"→"宝钢股份"）
    for k, v in _名码缓存.items():
        if k.replace("*ST", "").strip() == 名:
            return v
    for k, v in _名码缓存.items():
        if k and (k.startswith(名) or 名.startswith(k)):
            return v
    return None


def _拿表(代码: str, 表名: str):
    键 = (代码, 表名)
    now = time.time()
    if 键 in _报表缓存 and now - _报表缓存[键][1] < 300:
        return _报表缓存[键][0]
    df = _静默取(lambda: ak.stock_financial_report_sina(stock=代码, symbol=表名))
    _报表缓存[键] = (df, now)
    return df


# ══════════════════════════════════════════════════════════════
# 补丁（本地修复）：**关键科目概览** —— 兜住"最新财报情况"这类**没指定科目**的问法
# ══════════════════════════════════════════════════════════════
# 【修的是什么（前端实测）】
#   用户问「东吴证券最新财报情况」——没有具体科目。而 `查公开财报` 的 `指标` 是**必填**，
#   模型只好自己编一个（如"财报情况"）→ 三张表里都找不到这一列 →
#   工具回"找不到科目，可用科目示例：…" → 模型只好说"未获取到具体数据" → **等于没答**。
#
# 【修法】把"没给科目"当成一个**正当的输入**：直接汇总几个**核心科目**的最新一期数值。
#   这样"最新财报情况""整体怎么样""最近业绩如何"都能拿到真数据。
# ══════════════════════════════════════════════════════════════
# 概览要看的核心科目（按这个顺序展示；找不到的自动跳过）
_概览科目 = ("营业总收入", "营业收入", "归属于母公司所有者的净利润", "净利润",
             "资产总计", "负债合计", "基本每股收益", "经营活动产生的现金流量净额")
# 这些"没指定科目"的说法 → 一律走概览（注意：空串也在内）
_概览词 = {"", "概览", "情况", "最新", "最新财报", "财报", "整体", "整体情况",
           "主要指标", "关键指标", "摘要", "业绩", "业绩情况", "财务状况", "全部", "所有"}


def _日期化(报告日) -> str:
    """`20260630`（AKShare 报告日原样）→ `2026-06-30`（人/模型/溯源都能认的写法）。

    【为什么必须做这一步】（2026-10-19 实测的事故）
      AKShare 的 `报告日` 是**紧凑 8 位**。工具原样吐出去，模型在答案里自然会
      转述成「截至 2026年6月30日」，而 `关系判据.同源同组()` 要在**来源块**里
      找这个时点 —— 块里写的是 `20260630`，对不上 → 判「时点无出处」
      → **整条正确答案被硬拦**（「中国银行最新财报情况」就是这么被误杀的）。
      即：模型越规规矩矩标时点，越会被拦。
    两处一起修：这里把日期写规范（给人看的标准形态），
    `关系判据` 那边同时补了紧凑格式的识别（给别的数据源兜底）。
    """
    s = str(报告日).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _概览(代码: str, 报告期: str) -> str:
    """给"最新财报情况 / 整体怎么样"这类**没指定科目**的问题：汇总核心科目最新一期。"""
    期 = (报告期 or "").strip()
    条目: list[str] = []
    for 科目 in _概览科目:
        for 表名 in ("利润表", "资产负债表", "现金流量表"):
            try:
                df = _拿表(代码, 表名)
            except Exception as e:
                _日志(f"⚠️ 概览取 {代码} {表名} 失败：{type(e).__name__}: {e}")
                continue
            if df is None or df.empty or "报告日" not in df.columns:
                continue
            if 科目 not in df.columns:
                continue
            d = df.copy()
            d["报告日"] = d["报告日"].astype(str)
            if 期:
                d = d[d["报告日"] == 期]
            d = d.sort_values("报告日", ascending=False)
            d = d[d[科目].notna()]                      # 跳过空值期
            if d.empty:
                continue
            r = d.iloc[0]
            值 = r[科目]
            try:
                if "每股" in 科目:                       # 每股类：只报"元/股"，别换算成亿元
                    条目.append(f"  · {科目} = {值} 元/股（{表名}｜{_日期化(r['报告日'])}）")
                else:
                    条目.append(f"  · {科目} = {float(值) / 1e8:,.2f} 亿元"
                                f"（{值} 元｜{表名}｜{_日期化(r['报告日'])}）")
            except (TypeError, ValueError):
                条目.append(f"  · {科目} = {值}（{表名}｜{_日期化(r['报告日'])}）")
            break                                        # 该科目已找到，不用再试别的表

    if not 条目:
        return (f"公开财报取不到 {代码} 的任何关键科目（该代码可能没有公开三大报表）。"
                + (f"（限定报告期 {期}）" if 期 else ""))

    行 = [f"【公开财报·合并报表口径】{代码} ｜ 关键科目概览（最新一期）"]
    行 += 条目
    行.append("数据源：AkShare（新浪财经）｜口径：**合并报表**（对外披露口径，"
              "≠ 本地库的母公司个别报表，不可混用）。")
    行.append("⚠️ 这是「关键科目概览」；若要看**某个具体科目**，请指明科目名再查一次。")
    return "\n".join(行)


@mcp.tool(name="query_public_financials")
def 查公开财报(股票: str, 指标: str = "", 报告期: str = "") -> str:
    """查 A 股公司**公开财报（合并报表口径）**的财务数据 —— 用来看"正规对外披露是多少"。

    什么时候该用：
      · 用户明确要**公开口径 / 合并报表 / 对外披露**的数；
      · 用户说"我在东方财富/同花顺上看到的是 X"——那是合并报表，用本工具核对；
      · 需要把本地的**母公司（个别）报表**数拿去和外界对照时（本工具给"外面那一半"）；
      · 用户问"**最新财报情况 / 整体怎么样 / 业绩如何**"这类**没指定科目**的开放问法
        —— 这时**不要瞎填指标**，把 `指标` **留空**，本工具会返回**关键科目概览**。
    什么时候不该用：只想查本机库内数据（用 `查财务指标`，那才是库内口径）。

    参数：
      - 股票：代码（`600019.SH` / `sh600019` / `600019` 都行）或**公司名**（如 "宝钢股份"）。
      - 指标：中文科目名，如 "营业总收入" / "营业收入" / "归属于母公司所有者的净利润" /
        "资产总计" / "基本每股收益" / "经营活动产生的现金流量净额"。
        三张表会自动找（利润表 → 资产负债表 → 现金流量表）。
        ⭐ **留空 = 关键科目概览**（汇总上面几个核心科目的最新一期）——
          用户问"整体/情况/最新财报"而没点名科目时，**用留空，不要编一个科目名**。
      - 报告期：`20251231` 这种格式；留空取**最新一期**（可能是中报/季报，返回体会写明）。

    ⚠️ 返回体里会写清用哪一列匹配的、以及这是**合并报表**口径 —— 转述时必须一起说，
       不然用户会拿它跟本地母公司口径的数直接比，越比越乱。
    """
    if not _AK:
        return "【公开财报查询失败】akshare 未安装或导入失败，请 `pip install akshare` 后重启 MCP server"
    代码 = _代码化(股票)
    if 代码 is None:
        码 = _名转码(股票)
        if 码 is None:
            # ⚠️ 这里**不能只说"认不出"** —— 那会让模型直接放弃并告诉用户"查不到"
            #    （实测事故：问「中亿丰最新财报」，模型答"未查询到该公司最新财报的相关公开财务数据"，
            #      而同一只股票用代码 002333 一查就有完整中报）。
            #    ⇒ 必须给出**下一步可执行的指令**，让模型自己把这条路走通。
            return (f"认不出股票「{股票}」。\n"
                    f"· 直接用代码最稳：`600019.SH` / `sh600019` / `600019`；\n"
                    f"· 或用**证券简称**（如「宝钢股份」）。⚠️ **集团名/品牌名常常不等于证券简称**\n"
                    f"  （例：「中亿丰」是集团名，A 股上市主体的简称是「罗普斯金」、代码 002333）。\n"
                    f"👉 **如果你不确定代码，请先用 `web_search` 搜「{股票} 股票代码」，"
                    f"拿到代码后再调一次本工具** —— 不要直接回答“查不到”。")
        代码 = _代码化(码) or 码

    指标 = (指标 or "").strip()
    # 补丁（本地修复）：没给科目（或给的是"财报/情况/整体/最新…"这类泛词）→ 走**概览**，
    #   而不是回一句"请给出科目名"把球踢回给模型（那正是「最新财报情况」答不出来的原因）。
    if 指标 in _概览词:
        return _概览(代码, 报告期)

    期 = (报告期 or "").strip()
    候选列: list[str] = []
    命中 = None
    for 表名 in ("利润表", "资产负债表", "现金流量表"):
        try:
            df = _拿表(代码, 表名)
        except Exception as e:
            _日志(f"⚠️ 取 {代码} {表名} 失败：{type(e).__name__}: {e}")
            continue
        if df is None or df.empty:
            continue
        列 = [c for c in df.columns if c not in ("数据源", "是否审计", "公告日期", "币种", "类型", "更新日期")]
        if 指标 in 列:
            命中 = (表名, 指标, df)
            break
        候选列.extend([c for c in 列 if 指标 in c or c in 指标])

    if 命中 is None:
        if not 候选列:
            return (f"公开财报里找不到科目「{指标}」。可用科目示例：营业总收入、营业收入、"
                    f"归属于母公司所有者的净利润、净利润、资产总计、基本每股收益。")
        return ("公开财报里没有同名科目「" + 指标 + "」。最接近的候选："
                + "、".join(list(dict.fromkeys(候选列))[:6]) + "。请用其中一个名字重新提问。")

    表名, 列名, df = 命中
    if "报告日" not in df.columns:
        return f"取到的 {表名} 结构异常（没有报告日列），无法定位报告期。"
    d = df.copy()
    d["报告日"] = d["报告日"].astype(str)
    if 期:
        d = d[d["报告日"] == 期]
        if d.empty:
            return (f"公开财报里没有报告期 {期} 的记录（该期可能还没披露）。"
                    f"最新可选报告期："
                    + "、".join(_日期化(x) for x in sorted(df["报告日"].astype(str).unique())[-5:][::-1]))
    else:
        d = d.sort_values("报告日", ascending=False).head(1)
        if d.empty:
            return f"{代码} 的公开财报没取到任何报告期。"

    r = d.iloc[0]
    值 = r.get(列名)
    if 值 is None or str(值) in ("nan", "None", ""):
        # 该期该科目为空 → 往上找最近一个非空期，并**明确说是哪一期**
        d2 = df.copy()
        d2["报告日"] = d2["报告日"].astype(str)
        d2 = d2[d2["报告日"] <= str(r["报告日"])].sort_values("报告日", ascending=False)
        d2 = d2[d2[列名].notna()]
        if d2.empty:
            return (f"公开财报里「{列名}」（{表名}）在 {r['报告日']} 及之前都为空 —— "
                    f"该科目这家公司未披露。")
        r = d2.iloc[0]
        值 = r.get(列名)
        return (f"【公开财报·合并报表口径】{代码} ｜ {表名}「{列名}」\n"
                f"⚠️ 你要的报告期该科目**为空**；下面是**最近一个有值的报告期** {_日期化(r['报告日'])}：\n"
                f"  · {列名} = {值} 元（= {float(值) / 1e8:,.2f} 亿元）\n"
                f"数据源：AkShare（新浪财经）｜口径：**合并报表**（≠ 本地库的母公司个别报表）")

    try:
        亿 = f"{float(值) / 1e8:,.2f} 亿元"
    except (TypeError, ValueError):
        亿 = "（非数值，无法换算）"
    return (f"【公开财报·合并报表口径】{代码} ｜ {表名}「{列名}」\n"
            f"  · {列名} = {值} 元（= {亿}）\n"
            f"  · 报告期 {_日期化(r['报告日'])} ｜ 公告日期 {_日期化(r.get('公告日期', '—'))}\n"
            f"数据源：AkShare（新浪财经）｜⚠️ 口径：**合并报表**（对外披露口径），"
            f"与本地库的**母公司（个别）报表**是两套数，不可混用。")


# ══════════════════════════════════════════════════════════════
# ⭐ 2026-10-20 新增：**行业级公开（合并报表）聚合**
# ══════════════════════════════════════════════════════════════
# 【为什么要它（用户前端实测反馈，原话）】
#   「如果我数据库没有，他不应该切换工具例如 mcp 去搜索吗？」
#   用户问「钢铁行业 2012 年营业总收入合计」时看到的完整链路是：
#     · 本地 `行业聚合` → 明确返回「超出本库覆盖范围（本库只有 2023 年报 ~ 2026 一季报）」
#     · 然后……**就停了**。模型只会说"须逐家公司查"，或者反问"需要我逐家查吗？"
#   ⇒ 问题的根子不是"该不该换工具"，而是**没有可换的工具**：
#      `query_public_financials` 只支持单家公司，**行业级历史聚合这条路是断的**。
#      本工具把它接上：**行业成员（本地库）+ 逐家公开合并报表（AkShare）→ 汇总**。
#
# 【两个数据来源，各管一半】
#   ① **行业里有谁** = 本地库 `fr<行业>_history`（申万一级，28 个）—— 本地就有，不走外网；
#   ② **每家的数是多少** = AkShare `stock_financial_report_sina`（合并报表，实测可回溯 1997 年）。
#
# 【实测性能（别凭直觉改这些数，都是跑出来的）】
#   · 单家 0.6 ~ 2.5s；
#   · **新浪服务端吞吐上限约 9 家/秒**：并发 8→16→24→32，墙钟几乎不变
#     （机械设备 521 家：16 并发 56.5s，24 并发 56.7s，32 并发 58.7s，结果完全一致）——
#     说明瓶颈在**服务端限流**，本地加大并发没用。⇒ 所以默认 16 并发，不再往上堆。
#   · 钢铁 45 家：8 并发仅 8.4s；**机械设备 521 家要 ~57s，逼近 MCP 工具的 60s 超时**。
#   ⇒ 因此设了**墙钟预算**（默认 45s）：到点就停，并在返回体里**明确说完成了几家、
#     未完成时合计只能当"下界"**。大行业（机械设备/电子/医药生物/基础化工/电力设备/
#     计算机/汽车）在预算内跑不完，这不是 bug 而是外部接口的吞吐限制 ——
#     如实说明，**绝不用抽样外推冒充全量**（那才是真正的编造）。
#
# 【两个必须随数值一起转述的说明（漏了就是误导）】
#   ① 这是**合并报表**口径（≠ 本地库的母公司个别报表），绝对值**不可混用**；
#   ② 成员表是**当前快照** → 查历史年份有**幸存者偏差**：
#      实测武钢股份（2016 年被宝钢吸收合并）、鞍钢新轧、邯郸钢铁、莱钢股份、济南钢铁、
#      韶钢松山**都不在今天钢铁行业的成员表里**；反过来 2019 年后上市的公司（翔楼新材/
#      友发集团/广大特材/甬金股份）在表里但 2012 年没有数据。
#      ⇒ 历史年份的真实行业口径应略大于本结果，返**回体必须写明**。

# 并发 / 墙钟预算（可用环境变量覆盖；改之前先看上面那组实测数）
# 🔴 【预算为什么是 15（两次实测事故，别往上调）】
#   ① 一开始设 45s（= server.py 的 `端到端超时`）→ 医药生物（473 家）**整个请求被服务端掐断**，
#      用户看到「本次查询超过了服务端时限」—— 比"只查到一部分"更糟：连部分结果都没了。
#   ② 改成 25s 仍然会撞 —— 服务端 45s 要装下**四段**：分诊(≈8s) + 第一次 LLM(≈8s)
#      + 工具(25s) + 收尾 LLM(≈8s) = 49s > 45s。工具预算是**总超时里的一段**，不是全部。
#   ⇒ 正确算法：预算 ≤ 45(服务端) − 8(分诊) − 8(LLM) − 8(收尾) − 6(缓冲) ≈ 15s。
#   ⇒ 代价：成员 >135 家的行业（机械设备 521 / 电子 491 / 医药生物 473 / 基础化工 419 /
#     电力设备 376 / 计算机 339 / 汽车 294 / 轻工制造 161 / 建筑装饰 160 / 有色金属 144）
#     跑不完，**返回体会明确标注「合计只是下界」**。这是外部接口吞吐的硬限制
#     （实测并发 8→32 墙钟不变，瓶颈在新浪服务端，本地加并发没用），不是可调参绕过的。
_INDUSTRY_WORKERS = max(1, int(os.getenv("MCP_INDUSTRY_WORKERS", "16")))
_INDUSTRY_BUDGET = max(5.0, float(os.getenv("MCP_INDUSTRY_BUDGET_S", "15")))
# 成员超过这个数就**提前预警**（按实测吞吐 ~9 家/秒 估算能否在预算内跑完）
_INDUSTRY_HINT = max(1, int(_INDUSTRY_BUDGET * 9))
_行业成员缓存: dict = {}
_科目定位缓存: dict = {}

行业前缀, 行业后缀 = "fr", "_history"
_三大表 = ("利润表", "资产负债表", "现金流量表")

# 用户口语 → 公开财报里的**真实列名**（列名本身就是中文科目名，见文件顶注释）
_科目别名 = {
    "营收": "营业总收入", "收入": "营业总收入", "总收入": "营业总收入",
    "营业总收入": "营业总收入", "营业收入": "营业收入",
    "归母净利润": "归属于母公司所有者的净利润",
    "归母净利": "归属于母公司所有者的净利润",
    "净利润": "净利润",
    "总资产": "资产总计", "资产总额": "资产总计", "资产合计": "资产总计",
    "总负债": "负债合计", "负债总额": "负债合计", "负债总计": "负债合计",
    "净资产": "所有者权益(或股东权益)合计",
    "股东权益": "所有者权益(或股东权益)合计",
    "股东权益合计": "所有者权益(或股东权益)合计",
    "所有者权益": "所有者权益(或股东权益)合计",
    "每股收益": "基本每股收益", "EPS": "基本每股收益", "eps": "基本每股收益",
    "经营现金流": "经营活动产生的现金流量净额",
    "经营活动现金流": "经营活动产生的现金流量净额",
    "经营活动现金流量净额": "经营活动产生的现金流量净额",
    "现金流净额": "经营活动产生的现金流量净额",
}

# 比率类：**必须跨公司 Σ分子 ÷ Σ分母**（行业毛利率 ≠ 各家毛利率的简单平均）
# 定义 = (分子四元组, 分母四元组)；每项 = (列名, 系数)
_行业比率定义 = {
    "毛利率": ([("营业总收入", 1), ("营业成本", -1)], [("营业总收入", 1)]),
    "销售毛利率": ([("营业总收入", 1), ("营业成本", -1)], [("营业总收入", 1)]),
    "净利率": ([("净利润", 1)], [("营业总收入", 1)]),
    "销售净利率": ([("净利润", 1)], [("营业总收入", 1)]),
}

# 口语行业名 → 库里的申万一级名
_行业别名 = {
    "家电": "家用电器", "医药": "医药生物", "食品": "食品饮料", "饮料": "食品饮料",
    "有色": "有色金属", "化工": "基础化工", "建材": "建筑材料", "建筑": "建筑装饰",
    "军工": "国防军工", "石化": "石油石化", "农业": "农林牧渔", "农牧": "农林牧渔",
    "轻工": "轻工制造", "纺服": "纺织服饰", "纺织": "纺织服饰",
    "新能源": "电力设备", "半导体": "电子", "软件": "计算机", "互联网": "传媒",
    "通讯": "通信", "交运": "交通运输", "商贸": "商贸零售", "零售": "商贸零售",
    "地产": "房地产",
}


def _env值(名: str, 默认: str = "") -> str:
    """先环境变量、再同目录 .env —— **不 import config.py**，本 server 保持"零项目依赖"。

    （主进程 `server.py` 已把 .env 灌进 os.environ 并透传给本子进程；
      这里再读一遍是为了**直接命令行跑本文件时也能连库**，实测更稳。）
    """
    v = os.getenv(名)
    if v:
        return v
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(名 + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return 默认


def _连库():
    import pymysql
    return pymysql.connect(
        host=_env值("DB_HOST", "127.0.0.1"),
        port=int(_env值("DB_PORT", "3306") or 3306),
        user=_env值("DB_USER", "root"),
        password=_env值("DB_PASSWORD") or os.getenv("MYSQL_PWD", ""),
        database=_env值("DB_NAME", "workspace"),
        charset="utf8mb4", connect_timeout=10)


def _行业名单(cur) -> list[str]:
    """从 `fr<行业>_history` 表名反推库里真实存在的行业（申万一级）。"""
    cur.execute("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name LIKE %s",
                (_env值("DB_NAME", "workspace"), 行业前缀 + "%" + 行业后缀))
    出 = []
    for (t,) in cur.fetchall():
        if not t.endswith(行业后缀):
            continue
        名 = t[len(行业前缀):-len(行业后缀)]
        if 名 and not 名.startswith("_"):
            出.append(名)
    return sorted(出)


def _归一行业(名: str, 可选: list[str]) -> str | None:
    """把用户说的行业名归一成库里真实的行业名（只在**明确唯一**时归一，不猜）。"""
    名 = (名 or "").strip()
    if 名 in 可选:
        return 名
    for 尾 in ("行业", "板块"):
        if 名.endswith(尾):
            短 = 名[:-len(尾)]
            if 短 in 可选:
                return 短
            if _行业别名.get(短) in 可选:
                return _行业别名[短]
    if _行业别名.get(名) in 可选:
        return _行业别名[名]
    命中 = [t for t in 可选 if t.startswith(名) or t.endswith(名) or 名 in t]
    return 命中[0] if len(命中) == 1 else None


def _成员(cur, 行业: str) -> tuple[str, list[tuple[str, str]]]:
    """返回 (成员表真名, [(wind_code, 简称)])。表头两列就是 股票代码 / 股票简称。"""
    表 = 行业前缀 + 行业 + 行业后缀
    cur.execute(f"SELECT 股票代码, 股票简称 FROM `{表}` WHERE 股票代码 LIKE %s", ("%.%",))
    # ⚠️ 成员表是"Excel 导出形"，后面有大量空行（股票代码='0'），必须过滤
    return 表, [(a, b) for a, b in cur.fetchall() if a]


def _码(wind: str) -> str | None:
    """`000709.SZ` → `sz000709`（新浪接口要的格式）。"""
    s = (wind or "").strip()
    if "." in s:
        号, _, 所 = s.partition(".")
        if 号.isdigit() and 所.upper() in ("SH", "SZ", "BJ"):
            return 所.lower() + 号
    return None


def _认列(代码: str, 指标: str) -> tuple[str, str] | None:
    """在三大表里找这个科目在哪张表、叫什么列名。返回 (表名, 列名) 或 None。

    ⚠️ 只探测**一次**（用第一家成员），之后全行业复用同一个 (表, 列) ——
       否则每家公司都要试 3 张表，请求数直接 ×3（新浪那边会限流）。
    """
    if 指标 in _科目定位缓存:
        return _科目定位缓存[指标]
    目标 = _科目别名.get(指标, 指标)
    结果 = None
    for 表名 in _三大表:
        try:
            df = _拿表(代码, 表名)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        列 = list(df.columns)
        if 目标 in 列:
            结果 = (表名, 目标)
            break
        # 别名兜底：目标名在列里（"营业总收入" ⊂ 某列）或反之
        近 = [c for c in 列 if 目标 in c or c in 目标]
        if len(近) == 1:
            结果 = (表名, 近[0])
            break
    _科目定位缓存[指标] = 结果
    return 结果


def _取一家(代码: str, 表名: str, 列名: str, 期: str) -> tuple[str, float | None, str]:
    """取一家公司在指定报告期的一个科目值。返回 (代码, 数值或None, 原因)。"""
    try:
        df = _拿表(代码, 表名)
    except Exception as e:
        return 代码, None, f"{type(e).__name__}"
    if df is None or df.empty or "报告日" not in df.columns:
        return 代码, None, "无该表"
    if 列名 not in df.columns:
        return 代码, None, "无该列"
    d = df[df["报告日"].astype(str) == 期]
    if d.empty:
        return 代码, None, "该期无记录"
    v = d.iloc[0].get(列名)
    if v is None or str(v) in ("nan", "None", ""):
        return 代码, None, "该期该科目为空"
    try:
        return 代码, float(v), ""
    except (TypeError, ValueError):
        return 代码, None, "非数值"


def _取一家多列(代码: str, 表名: str, 列们: list[str], 期: str):
    """取一家公司的多个列（比率聚合用；**要求全部列都有值**，否则这一家不参与）。"""
    try:
        df = _拿表(代码, 表名)
    except Exception:
        return None
    if df is None or df.empty or "报告日" not in df.columns:
        return None
    for c in 列们:
        if c not in df.columns:
            return None
    d = df[df["报告日"].astype(str) == 期]
    if d.empty:
        return None
    行 = d.iloc[0]
    出 = {}
    for c in 列们:
        v = 行.get(c)
        if v is None or str(v) in ("nan", "None", ""):
            return None
        try:
            出[c] = float(v)
        except (TypeError, ValueError):
            return None
    return 出


def _并发取(项: list, 并发: int, 预算: float):
    """带**墙钟预算**的并发执行。到点就停，返回 (完成的结果, 是否被打断)。

    🔴 【实测踩坑：预算曾经是假的】
      一开始写成 `with ThreadPoolExecutor(...) as ex:` —— 而它的 `__exit__` 会调
      `shutdown(wait=True)`，**把已经提交的任务全部 join 完**才返回。
      后果：机械设备 521 家、预算设 45s，实际仍要 **56.5s** 才返回（预算完全没用）。
      ⇒ 必须显式 `shutdown(wait=False, cancel_futures=True)`：
         取消**还没开始**的任务、**不阻塞**等已在跑的那几个。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    出 = []
    被打断 = False
    if not 项:
        return 出, 被打断
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=max(1, 并发))
    try:
        未来 = {ex.submit(函数, *参数): 参数 for 函数, 参数 in 项}
        for f in as_completed(未来):
            出.append(f.result())
            if time.time() - t0 > 预算:
                被打断 = True
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return 出, 被打断


def _同比率(极名, 指标):
    """把比率名归一（"行业毛利率"/"平均净利率" → "毛利率"/"净利率"）。"""
    for k in ("毛利率", "净利率"):
        if k in (指标 or ""):
            return "毛利率" if k == "毛利率" else "净利率"
    return None


@mcp.tool(name="query_industry_public_financials")
def 查行业公开财报(行业: str, 指标: str, 年份: int = 0, 口径: str = "合计") -> str:
    """查某个**行业整体**在**历史年份**的财务数据 —— 公开合并报表口径，逐家公司取回后汇总。

    什么时候该用（**这正是本地库的盲区**）：
      · 问**整个行业**的财务数，且**年份早于 2023 年报**（本地 `行业聚合` 只有 2023 年报~
        2026 一季报，更早的年份会明确返回"超出本库覆盖范围"）；
        例：「钢铁行业 2012 年营业总收入合计」「医药生物行业 2018 年平均毛利率」。
      · 想用**公开合并报表口径**看行业规模（本地库是母公司个别报表，绝对量不可比）。
    什么时候不该用：
      · 年份在 2023 年报 ~ 2026 一季报之间 → **优先用本地 `行业聚合`**（更快、且是库内权威口径）；
      · 问**单家公司** → 用 `query_public_financials`（单家、更快）；
      · 问行情/涨跌幅 → 用 `查板块`。

    参数：
      - 行业：**申万一级行业名**（"钢铁"/"医药生物"/"电子"，共 28 个）。
      - 指标：**金额类科目**（营业总收入 / 营业收入 / 归属于母公司所有者的净利润 / 净利润 /
        资产总计 / 负债合计 / 所有者权益(或股东权益)合计 / 基本每股收益 /
        经营活动产生的现金流量净额…），也支持**毛利率 / 净利率**两个比率。
      - 年份：报告期年份（如 2012）；留空取**最新一期年报**。
      - 口径：合计 / 均值 / 中位数 / 加权（比率类才有加权）。**能算的都会列出来**，此参数只标"采用"。

    ⚠️ 返回体里有三件事必须一起转述（漏了就是把用户带偏）：
      ① 这是**合并报表**口径，与本地库的母公司个别报表**不可直接比较**；
      ② 成员表是**当前快照** → 历史年份存在**幸存者偏差**（已退市的当年成员不在内、
         后来上市的当年没有数据），真实历史口径应略大于本结果；
      ③ 若返回体提示"预算内未跑完"，合计只能当**下界**看，**不得**说成行业完整合计。
    """
    行业 = (行业 or "").strip()
    指标 = (指标 or "").strip()
    口径 = (口径 or "合计").strip() or "合计"
    if not 行业 or not 指标:
        return "请同时给出**行业**与**指标**（例：行业=钢铁，指标=营业总收入，年份=2012）。"
    if not _AK:
        return "【行业公开财报查询失败】akshare 未安装或导入失败，请 `pip install akshare` 后重启 MCP server"

    比值 = _同比率(None, 指标)
    # ⚠️ 必须用**归一后的比值**去查定义，不能用原始指标名 ——
    #   否则「钢铁行业毛利率」这种带前缀的说法会查不到定义，掉进"找列"分支报错。
    比率定义 = _行业比率定义.get(比值) if 比值 else None

    # ── ① 行业成员（本地库，不走外网）──
    try:
        conn = _连库()
    except Exception as e:
        return (f"【行业公开财报】本地库连不上（{type(e).__name__}），拿不到行业成员名单。"
                f"请确认本机 MySQL 已启动（3306）。")
    try:
        with conn.cursor() as cur:
            可选 = _行业名单(cur)
            真行业 = _归一行业(行业, 可选)
            if 真行业 is None:
                return (f"认不出行业「{行业}」。可用行业（共 {len(可选)} 个，申万一级）："
                        + "、".join(可选))
            成员表, 成员们 = _成员(cur, 真行业)
    finally:
        conn.close()
    if not 成员们:
        return f"行业「{真行业}」的成员表（`{成员表}`）是空的，无法聚合。"
    码们 = [(c := _码(w), n) for w, n in 成员们]
    码们 = [(c, n) for c, n in 码们 if c]

    期 = f"{int(年份)}1231" if 年份 else None

    # ── ② 定位科目（拿第一家探一次，之后全行业复用，省 2/3 请求）──
    if 比率定义:
        列们 = [f for f, _ in 比率定义[0] + 比率定义[1]]
        定位 = None
        for 表名 in _三大表:
            try:
                df = _拿表(码们[0][0], 表名)
            except Exception:
                continue
            if df is not None and not df.empty and all(c in df.columns for c in 列们):
                定位 = (表名, None)
                break
        if 定位 is None:
            return (f"公开财报里算不出「{指标}」（需要 {'、'.join(列们)} 这几列，"
                    f"在该公司报表里没找全）。")
    else:
        定位 = _认列(码们[0][0], 指标)
        if 定位 is None:
            return (f"公开财报里找不到科目「{指标}」。金额类可用示例：营业总收入、营业收入、"
                    f"归属于母公司所有者的净利润、净利润、资产总计、负债合计、基本每股收益。")
    表名, 列名 = 定位

    # ── ③ 若没给年份，先探最新年报期 ──
    if 期 is None:
        try:
            df = _拿表(码们[0][0], 表名)
            d = sorted(str(x) for x in df["报告日"].astype(str).unique() if str(x).endswith("1231"))
            期 = d[-1] if d else None
        except Exception:
            期 = None
        if 期 is None:
            return f"取不到「{指标}」的报告期（该表没有年报记录）。"

    # ── ④ 并发逐家取（**带墙钟预算**）──
    t0 = time.time()
    if 比率定义:
        (分子元, 分母元) = 比率定义
        列们 = list(dict.fromkeys([f for f, _ in 分子元 + 分母元]))
        任务 = [(_取一家多列, (c, 表名, 列们, 期)) for c, _ in 码们]
        结果, 被打断 = _并发取(任务, _INDUSTRY_WORKERS, _INDUSTRY_BUDGET)
        样本 = []
        缺失 = {}
        for r in 结果:
            if r is None:
                缺失["无该期/无该列"] = 缺失.get("无该期/无该列", 0) + 1
                continue
            分子 = sum(s * r[f] for f, s in 分子元)
            分母 = sum(s * r[f] for f, s in 分母元)
            样本.append((分子, 分母))
    else:
        任务 = [(_取一家, (c, 表名, 列名, 期)) for c, _ in 码们]
        结果, 被打断 = _并发取(任务, _INDUSTRY_WORKERS, _INDUSTRY_BUDGET)
        样本 = []
        缺失 = {}
        for _, v, 因 in 结果:
            if v is None:
                缺失[因 or "未知"] = 缺失.get(因 or "未知", 0) + 1
            else:
                样本.append(v)
    墙钟 = time.time() - t0
    完成 = len(结果)
    取到 = len(样本)

    # ── ⑤ 组装返回体 ──
    是比率 = bool(比率定义)
    行 = [f"【行业公开财报·合并报表口径】{真行业}行业 · {str(期)[:4]} 年报 · 「{指标}」",
          f"成员来源：本地库 `{成员表}`（申万一级，当前快照）｜ 表内成员 {len(成员们)} 只 "
          f"｜ 可识代码 {len(码们)}｜ 本次取到值 {取到}/{完成} 家（用时 {墙钟:.1f}s）"]
    # ⚠️ 成员很多的行业：**开跑前就说明"大概率跑不完"**，别等结果出来才补一句
    if len(码们) > _INDUSTRY_HINT:
        行.append(f"ℹ️ 该行业成员较多（{len(码们)} 家 > 预算内可完成约 {_INDUSTRY_HINT} 家），"
                  f"受外部接口吞吐限制，本次**仅供参考**：下面的合计大概率**不是完整值**。")

    if 取到 == 0:
        行.append(f"⚠️ 没有一家取到「{指标}」的数值。失败原因分布：{缺失}")
        行.append("可能原因：① 该年份太早、多数成员当年还没上市；② 科目名不对。"
                  "请换一个更常见的科目（营业总收入 / 资产总计）或换年份再试。")
        行.append("数据源：AkShare（新浪财经）· 合并报表口径")
        return "\n".join(行)

    if 是比率:
        分子和 = sum(a for a, _ in 样本)
        分母和 = sum(b for _, b in 样本)
        加权 = 分子和 / 分母和 * 100 if 分母和 else 0.0
        各家 = [a / b * 100 for a, b in 样本 if b]
        均值 = sum(各家) / len(各家) if 各家 else 0.0
        中位 = sorted(各家)[len(各家) // 2] if 各家 else 0.0
        行.append("口径：")
        行.append(f"  · 加权   = {加权:.2f}%   （**Σ分子 ÷ Σ分母**：跨公司先把分子、分母分别"
                  f"求和再相除 —— 这才是**行业真实水平**）"
                  + ("   ← 采用" if 口径 in ("加权", "合计") else ""))
        行.append(f"  · 均值   = {均值:.2f}%   （成员**简单平均**，小公司会拉偏）"
                  + ("   ← 采用" if 口径 == "均值" else ""))
        行.append(f"  · 中位数 = {中位:.2f}%" + ("   ← 采用" if 口径 == "中位数" else ""))
        行.append(f"  ⚠️ 「简单平均 {均值:.2f}%」与「加权 {加权:.2f}%」"
                  f"{'一致' if abs(均值 - 加权) < 0.01 else f'相差 {加权 - 均值:+.2f} 个百分点'} —— "
                  f"行业口径**应引用加权**，简单平均仅供对照。")
    else:
        合计 = sum(样本)
        均值 = 合计 / 取到
        中位 = sorted(样本)[取到 // 2]
        行.append("口径：")
        行.append(f"  · 合计   = {合计 / 1e8:,.2f} 亿元"
                  + ("   ← 采用" if 口径 == "合计" else ""))
        行.append(f"  · 均值   = {均值 / 1e8:,.2f} 亿元   （成员简单平均）"
                  + ("   ← 采用" if 口径 == "均值" else ""))
        行.append(f"  · 中位数 = {中位 / 1e8:,.2f} 亿元"
                  + ("   ← 采用" if 口径 == "中位数" else ""))

    # 🔴 未跑完 → 必须说清"合计是下界"（绝不把部分的和说成全量）
    if 被打断 or 完成 < len(码们):
        行.append(f"🔴⚠️ **本次未跑完**：全行业 {len(码们)} 家，本次完成 {完成} 家"
                  f"（{_INDUSTRY_BUDGET:.0f}s 预算内，受外部接口吞吐限制）。"
                  f"因此上面的**「合计」只是已取到的 {取到} 家的和 = 下界**，"
                  f"**不是行业完整合计** —— 转述时必须说明这一点，不得说成行业总量。")

    if 缺失:
        行.append(f"未取到值的 {len(结果) - 取到} 家原因：{缺失}"
                  f"（多为该年份尚未上市 / 当年未披露该科目）。")
    行.append("⚠️ 口径：**合并报表**（对外披露口径），与本地库的**母公司（个别）报表**"
              "是两套数，**不可直接比较**。")
    if str(期)[:4] != str(__import__("datetime").date.today().year):
        行.append(f"⚠️ 成分股说明：成员取自成员表的**当前快照**（无历史时点版本）。"
                  f"查 {str(期)[:4]} 年时，**当年在、后来退市的公司不在内**"
                  f"（如武钢股份 2016 年被宝钢吸收合并，现已不在钢铁成员表），"
                  f"而**后来上市的公司在内但当年无数据** —— 存在**幸存者偏差**，"
                  f"真实历史行业口径应**略大于**本结果，转述时请一并说明。")
    行.append("数据源：AkShare（新浪财经，合并报表）· 行业成员来自本机 MySQL workspace")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ⚠️ 本 server 现在管三类东西：宏观指标（LPR / CPI）+ **公开财报**（合并报表，单家）
#    + **行业级公开财报聚合**（合并报表，跨公司）。共同点是"**外部来源、与本地库无关**"
#    → `mcp_client.py` 里已把它改名为「外部数据」。
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("[外部数据] MCP server 启动（stdio）", file=sys.stderr)
    mcp.run()
