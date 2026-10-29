# market_tool.py —— v5 第 8 讲：按《需求地图》那张 45% 的表，把系统"做宽"。
#
# 【为什么会有这个文件】
#   《需求地图》实测 1410 条真实流量：**45% 的问题要的是"实时接口"**，
#   而本系统此前只有 1 个实时工具（查实时行情）。这一讲就是把缺口补上。
#
# 【它和 realtime_tool.py 的关系（重要：不要重复造取数层）】
#   · 取数（requests + 重试 + 指数退避）、交易时段判断，**全部复用** realtime_tool：
#       `from realtime_tool import _取文本, _行情状态`
#     —— 一个系统里"怎么取数"只该有一份实现，否则重试策略/Referer 规则会各写各的、早晚不一致。
#   · 名字解析（中文名/代码 → wind_code）复用 stock_names（C 路线产出的全市场名单）。
#
# 【每个工具都必须守的三条纪律（v5 前面几讲挣来的）】
#   ① **绝不抛异常**：外部依赖（网络/接口）全在这里兜住，返回"人话"，让模型如实转述。
#      （v5 第 2 讲知识点 7：别让外部故障变成接口故障。）
#   ② **必须带出处 + 时点**：返回值里写清"数据源"和"数据时点/交易日"。
#      否则模型会把"昨天的"当"现在的"说出去（v5 第 1 讲知识点 2）。
#   ③ **必须定标**：外部接口的单位从来不告诉你（价格可能 ×1000、金额可能 ×10000），
#      不换算就会差几个数量级（v5 第 1 讲知识点 3）。
#
# 【实测踩到的两个"源会挂"的坑，已写进对应工具】
#   · `push2 stock/get`（个股快照那条路径）与 `push2his`（历史K线主机）在本机被风控，
#     实测 0/8 全挂 → 全部避开；改用 `clist` / `fflow` / 腾讯 `fqkline`。
#   · 涨停池 `push2ex` **必须带 `date` 参数**，否则返回 `data=null`。

import json
import re
from datetime import datetime, date

import requests
from langchain_core.tools import tool

from stock_names import 解析公司
from realtime_tool import _取文本, _行情状态

_UA_头 = {"Referer": "https://quote.eastmoney.com/"}

# 全市场 A 股（深主板 + 创业板 + 沪主板 + 科创板 + 北交所）
_FS全市场 = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


# ══════════════════════════════════════════════════════════════
# 通用小工具
# ══════════════════════════════════════════════════════════════
def _secid(代码: str) -> str:
    """600019.SH → 1.600019；000063.SZ → 0.000063；920982.BJ → 0.920982。"""
    号, 交 = 代码.split(".")
    return f"{'1' if 交 == 'SH' else '0'}.{号}"


def _亿(v) -> str:
    """元 → 亿/万（自动选单位）。定标 + 可读性一起解决。"""
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f} 万"
    return f"{v:.0f}"


def _f(v, 位: int = 2, 后缀: str = "", 默认: str = "—") -> str:
    """**安全数字格式化** —— 外部字段经常是 None（实测 TURNOVERRATE 就是 None）。
    ⚠️ 直接用 f"{x:.2f}" 遇 None 会抛 TypeError，那就违反了"工具绝不抛异常"的纪律①。
    所以所有"可能为 None 的数值"都必须走这里。"""
    if v is None or v == "":
        return 默认
    try:
        return f"{float(v):.{位}f}{后缀}"
    except Exception:
        return 默认


def _东财(url: str, 来源页: str = "https://quote.eastmoney.com/") -> dict | None:
    """取 JSON。走 realtime_tool 的 _取文本（带重试/退避），失败返回 None。"""
    文 = _取文本(url, 来源页=来源页)
    if not 文:
        return None
    try:
        return json.loads(文)
    except Exception:
        return None


def _认标的(标的: str):
    """返回 (wind_code, 显示名)；认不出返回 (None, 提示语)。"""
    代码 = 解析公司(标的)
    if not 代码:
        return None, (f"认不出标的「{标的}」。请用公司名（如 宝钢股份）、"
                      f"wind_code（如 600019.SH）或 6 位代码（如 600019）。")
    return 代码, 标的


def _时点横幅() -> str:
    状态 = _行情状态()
    return f"（数据时点：{datetime.now():%Y-%m-%d %H:%M} ｜ 当前 {状态}）"


def _规范日期(s: str) -> str | None:
    """把 2026-03-19 / 20260319 / 2026/3/19 归一成 'YYYY-MM-DD'；认不出返回 None。

    ⭐ v5-10：接口工具的历史日期参数统一走这里 —— 模型给日期的格式很随意
    （"2026-03-19"、"20260319"、"2026/3/19" 都见过），必须容错，否则用户问
    "3月19日的资金流"会因为格式不对而查不到。
    """
    s = (s or "").strip()
    m = re.match(r"^(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})$", s)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _资金流_新浪(代码: str, 标的: str) -> str:
    """备用源：新浪资金流（daima 用 sh600019 格式）。
    实测字段：opendate 日期 / netamount 净流入(元) / r0_net 特大单净额(元) / turnover 换手。
    ⚠️ 口径与东财不同 → 输出必须写明「新浪口径」，且日期可能滞后一个交易日（照搬返回日期）。"""
    号, 交 = 代码.split(".")
    信 = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[交] + 号
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=1&sort=opendate&asc=0&daima={信}")
    文 = _取文本(url, 来源页="https://finance.sina.com.cn/")
    if not 文:
        return (f"没取到 {标的}（{代码}）的资金流数据 —— 东财与新浪两个源都没响应，"
                f"请稍后再试，**不要凭记忆作答**。")
    try:
        列 = json.loads(文)
        行0 = 列[0] if isinstance(列, list) and 列 else None
    except Exception:
        行0 = None
    if not 行0:
        return f"没取到 {标的}（{代码}）的资金流数据（新浪返回为空）。"
    净, 特大 = 行0.get("netamount"), 行0.get("r0_net")
    数 = lambda v: abs(float(v)) if v not in (None, "") else 0
    方向 = lambda v: "净流入" if (v not in (None, "") and float(v) > 0) else \
                     ("净流出" if (v not in (None, "") and float(v) < 0) else "持平")
    return "\n".join([
        f"【当日资金流】{标的}（{代码}）（东财不可用，已切换新浪备用源）",
        f"数据源：新浪财经 资金流 ｜ 数据日期：{行0.get('opendate') or '未知'}"
        f"（⚠️ 新浪口径，可能与东财略有差异，日期也可能非当日）",
        # ⚠️ 新浪返回里还有个 `turnover` 字段，但实测它与宝钢真实换手率（~0.5%）对不上，
        #    语义没核实清楚 → **宁可不写，也不乱标**（这就是"未实测不写"）。
        f"{方向(净)} {_亿(数(净))}元 ｜ 特大单 {方向(特大)} {_亿(数(特大))}元",
    ])


def _资金流_历史(代码: str, 标的: str, 日期: str) -> str:
    """**历史某一日**的资金流。源：新浪 MoneyFlow.ssl_qsfx_zjlrqs（`num=360` 可回溯约 1 年）。

    ⭐ v5-10 新增（对应需求地图高频："3月19日主力资金流向"）。
       · 为什么用新浪而不用东财 `push2his` 日线：实测 push2his **在本机被风控**（同一 URL
         上一轮返 121 行、下一轮就空）→ 不能当主源。新浪这个接口稳定且带日期。
       · ⚠️ 新浪此接口**只给 netamount(主力净额) + r0_net(特大单净额)**，没有四类单拆分
         → 输出里如实说明，**不编造缺失字段**。
    """
    日 = _规范日期(日期)
    if not 日:
        return (f"日期「{日期}」看不懂。请用 2026-03-19 或 20260319 这种格式；"
                f"或留空查当日资金流。")
    号, 交 = 代码.split(".")
    信 = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[交] + 号
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=360&sort=opendate&asc=0&daima={信}")
    文 = _取文本(url, 来源页="https://finance.sina.com.cn/")
    if not 文:
        return (f"没取到 {标的}（{代码}）的历史资金流 —— 新浪源没响应，"
                f"请稍后重试，**不要凭记忆作答**。")
    try:
        列 = json.loads(文)
    except Exception:
        列 = None
    if not isinstance(列, list) or not 列:
        return f"没取到 {标的}（{代码}）的历史资金流（新浪返回为空）。"
    行 = next((x for x in 列 if (x.get("opendate") or "") == 日), None)
    if not 行:
        最早, 最新 = 列[-1].get("opendate"), 列[0].get("opendate")
        return (f"新浪资金流里没有 {标的}（{代码}）{日} 的记录 —— 可能该日非交易日，"
                f"或超出可回溯范围（{最早} ~ {最新}）。**请核对日期，不要编数据**。")
    净, 特大 = 行.get("netamount"), 行.get("r0_net")
    数 = lambda v: abs(float(v)) if v not in (None, "") else 0
    方向 = lambda v: "净流入" if (v not in (None, "") and float(v) > 0) else \
                     ("净流出" if (v not in (None, "") and float(v) < 0) else "持平")
    return "\n".join([
        f"【资金流·历史日】{标的}（{代码}）",
        f"数据源：新浪财经 资金流 ｜ 数据日期：{行.get('opendate')}",
        f"（⚠️ 新浪口径，仅 主力净额/特大单净额 两项；不含大单/中单/小单拆分）",
        f"主力 {方向(净)} {_亿(数(净))}元 ｜ 特大单 {方向(特大)} {_亿(数(特大))}元",
    ])


# ══════════════════════════════════════════════════════════════
# ① 个股资金流（对应需求地图最大桶：258 条 / 18.3%）
#    源：push2 fflow/kline（klt=1，返回全天逐分钟累计；取最后一行 = 当日累计）
#    实测字段：时间, 主力净流入, 小单, 中单, 大单, 超大单（单位：元）
#    ⚠️ 校验关系：大单 + 超大单 = 主力；小单 + 中单 = −主力（实测成立，可当断言用）
# ══════════════════════════════════════════════════════════════
@tool
def 查资金流(标的: str, 日期: str = "") -> str:
    """查某只 A 股的**资金流**：主力/超大单/大单/中单/小单的净流入额。

    何时该用：用户问"主力资金动向""今天资金流入多少""主力净买入额""大单/小单流向"，
    或问**某个历史日期**的资金流（如"3月19日主力资金流向""昨天主力资金"）。
    何时不该用：问股价（用 查实时行情）。

    参数：
      - 标的：公司名 / wind_code / 6 位代码。
      - 日期：可选。形如 "2026-03-19" 或 "20260319"；**留空 = 当日（最近交易日）**。
        ⚠️ 给了历史日期 → 走**新浪源**（可回溯约 1 年），只给"主力净额 + 特大单净额"；
           留空当日 → 走**东财源**，给完整的 主力/超大/大/中/小 单拆分。

    返回带数据源与时点（或交易日）的资金流数据。
    ⚠️ 非交易时段拿到的是**当日（最近交易日）收盘累计**。
    """
    代码, 提示 = _认标的(标的)
    if not 代码:
        return 提示
    if 日期.strip():
        return _资金流_历史(代码, 标的, 日期.strip())
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?"
           f"secid={_secid(代码)}&lmt=0&klt=1"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
    j = _东财(url)
    kl = ((j or {}).get("data") or {}).get("klines") or []
    if not kl:
        # ⚠️ 备用源：东财 push2 整站会被限流（实测"打着打着就全站不通"）→ 换新浪资金流。
        #    新浪口径与东财不同（netamount=净流入、r0_net=特大单净额），必须**标明口径**，
        #    而且它的日期可能滞后一个交易日（返回里有日期，要照搬）。
        return _资金流_新浪(代码, 标的)
    末 = kl[-1].split(",")
    if len(末) < 6:
        return f"{标的} 的资金流返回格式异常：{kl[-1]}"
    时间, 主力, 小单, 中单, 大单, 超大单 = 末[:6]
    数 = lambda s: float(s) if s not in ("", "-") else None
    主力, 小单, 中单, 大单, 超大单 = map(数, (主力, 小单, 中单, 大单, 超大单))
    方向 = lambda v: "净流入" if (v or 0) > 0 else ("净流出" if (v or 0) < 0 else "持平")
    行 = [f"【当日资金流】{标的}（{代码}）{_时点横幅()}",
          f"数据源：东方财富 push2 资金流 ｜ 截至：{时间}",
          f"主力 {方向(主力)} {_亿(abs(主力) if 主力 else 0)}元",
          f"　超大单 {方向(超大单)} {_亿(abs(超大单) if 超大单 else 0)}元 ｜ "
          f"大单 {方向(大单)} {_亿(abs(大单) if 大单 else 0)}元",
          f"　中单 {方向(中单)} {_亿(abs(中单) if 中单 else 0)}元 ｜ "
          f"小单 {方向(小单)} {_亿(abs(小单) if 小单 else 0)}元"]
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ② 全市场排行（对应"全市场筛选/选股" 82 条 + "哪些股票"类）
#    源：push2 clist，靠 (fid=排序字段, po=升降) 组合
#    ⚠️ 只取"需要的 top N"，不要把全市场 5900 条倒给模型（上下文会爆，也没意义）
# ══════════════════════════════════════════════════════════════
_榜表 = {
    "涨幅榜": ("f3", 1), "跌幅榜": ("f3", 0),
    "成交额榜": ("f6", 1), "换手率榜": ("f8", 1), "振幅榜": ("f7", 1),
}
# 备用源（东财 clist 会被限流 —— 实测打着打着就 0/8）：
# 新浪 Market_Center.getHQNodeData，字段 amount(元)/changepercent(%)/turnoverratio(%)。
_新浪排序 = {
    "涨幅榜": ("changepercent", 0), "跌幅榜": ("changepercent", 1),
    "成交额榜": ("amount", 0), "换手率榜": ("turnoverratio", 0),
}


def _排行_新浪(榜: str, 数量: int):
    """返回 [(代码, 名称, 最新价, 涨跌幅, 成交额)] 或 None。"""
    if 榜 not in _新浪排序:
        return None
    sort, asc = _新浪排序[榜]
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"Market_Center.getHQNodeData?page=1&num={数量}&sort={sort}&asc={asc}"
           "&node=hs_a&symbol=&_s_r_a=page")
    文 = _取文本(url, 来源页="https://finance.sina.com.cn/")
    if not 文:
        return None
    try:
        列 = json.loads(文)
    except Exception:
        return None
    if not isinstance(列, list) or not 列:
        return None
    出 = []
    for x in 列:
        出.append((x.get("code"), x.get("name"), x.get("trade"),
                   x.get("changepercent"), x.get("amount")))
    return 出


@tool
def 查排行(榜: str = "涨幅榜", 数量: int = 10) -> str:
    """查 A 股**全市场排行**：涨幅榜 / 跌幅榜 / 成交额榜 / 换手率榜 / 振幅榜。

    何时该用：用户问"今天涨得最好的股票""跌幅榜""成交额最大""放量""哪些股票涨得多/跌得多"。
    何时不该用：问某一只股票（用 查实时行情）；问涨停股票池（用 查涨停池，带连板/封板资金）。

    参数：
      - 榜：涨幅榜（默认）/ 跌幅榜 / 成交额榜 / 换手率榜 / 振幅榜。
      - 数量：返回前几名（默认 10，建议 ≤ 30）。

    返回排行榜文字（代码、名称、最新价、涨跌幅、成交额），带数据源与时点。
    """
    if 榜 not in _榜表:
        return f"不支持的榜「{榜}」。可选：{'、'.join(_榜表)}"
    数量 = max(1, min(int(数量 or 10), 30))
    fid, po = _榜表[榜]

    # ① 首选东财 clist
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           f"pn=1&pz={数量}&po={po}&np=1&fltt=2&invt=2&fid={fid}&fs={_FS全市场}"
           "&fields=f12,f14,f2,f3,f6")
    j = _东财(url)
    列 = ((j or {}).get("data") or {}).get("diff") or []
    来源 = "东方财富 clist"
    if not 列:
        # ② 东财挂了 → 换新浪（实测可用）
        备 = _排行_新浪(榜, 数量)
        if not 备:
            return (f"没取到{榜}（东财与新浪两个源都没响应）—— 可能接口抽风或非交易日，"
                    f"请稍后再试，**不要凭记忆编排名**。")
        行 = [f"【{榜}】前 {len(备)} 名 {_时点横幅()}", "数据源：新浪财经（东财不可用，已切换备用源）"]
        for i, (码, 名, 价, 涨, 额) in enumerate(备, 1):
            行.append(f"{i:>2}. {名}（{码}）  {_f(价)}  涨跌 {_f(涨)}%  "
                      f"成交额 {_亿(额)}")
        return "\n".join(行)

    行 = [f"【{榜}】前 {len(列)} 名 {_时点横幅()}", f"数据源：{来源}"]
    for i, x in enumerate(列, 1):
        行.append(f"{i:>2}. {x.get('f14') or '?'}（{x.get('f12')}）  {x.get('f2')}  "
                  f"涨跌 {x.get('f3')}%  成交额 {_亿(x.get('f6'))}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ③ 涨停池（"今天有多少涨停股票"这类，需求地图里高频）
#    源：push2ex getTopicZTPool
#    ⚠️ 实测必须带 date，否则 data=null
#    实测字段：c 代码 / n 名称 / zdp 涨跌幅 / p 价格(×1000) / lbc 连板数 /
#             fbt 首次封板时间(HHMMSS) / fund 封板资金 / hybk 行业 / zbc 炸板次数
# ══════════════════════════════════════════════════════════════
@tool
def 查涨停池(日期: str = "") -> str:
    """查 A 股**涨停股票池**：涨停家数 + 涨停名单（含连板数、封板资金、所属行业）。

    何时该用：用户问"今天有多少涨停股票""涨停的有哪些""连板股""哪个板块涨停多"。
    何时不该用：问涨幅榜（用 查排行）。

    参数：日期 —— 形如 "20261014"；不填则取今天。

    返回涨停总数与名单（最多 20 只，按连板数降序），带数据源与交易日。
    """
    日 = (日期 or "").strip() or date.today().strftime("%Y%m%d")
    url = ("https://push2ex.eastmoney.com/getTopicZTPool?"
           "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=100&sort=zdp%3Adesc&date={日}")
    j = _东财(url, 来源页="https://quote.eastmoney.com/ztb/detail")
    d = (j or {}).get("data") or {}
    pool = d.get("pool") or []
    if not pool:
        return (f"没取到 {日} 的涨停池 —— 可能是非交易日、接口抽风，或该日无涨停。"
                f"请核对日期后重试，不要编造涨停名单。")
    pool.sort(key=lambda x: (-(x.get("lbc") or 0), -(x.get("zdp") or 0)))
    行 = [f"【涨停池】{d.get('qdate') or 日} 共 {d.get('tc', len(pool))} 只涨停"
         f"（下列按连板数排序，取前 20）", "数据源：东方财富 push2ex"]
    for x in pool[:20]:
        封 = x.get("fbt") or 0
        封时 = f"{封 // 10000:02d}:{封 // 100 % 100:02d}" if 封 else "—"
        行.append(f"· {x.get('n')}（{x.get('c')}） 涨{_f(x.get('zdp'))}%  "
                  f"连板 {x.get('lbc') or 0} ｜ 封板 {封时} ｜ 封单 {_亿(x.get('fund'))} ｜ "
                  f"{x.get('hybk') or '—'}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ④ 板块行情（行业 / 概念）
#    源：push2 clist，fs=m:90+t:2（行业）/ m:90+t:3（概念）
#    字段 f104 上涨家数 / f105 下跌家数 / f128 领涨股 / f136 领涨股涨跌幅
# ══════════════════════════════════════════════════════════════
# 备用源：腾讯板块（东财 clist 会被限流）。实测 board_type=hy(行业) / gn(概念) 都可用。
#   字段：name 名称 / zdf 板块涨跌幅 / zgb "涨家数/跌家数" / lzg{name,zdf} 领涨股。
_腾讯板 = {"行业": "hy", "概念": "gn"}
_板缓存: dict = {}          # {类型: (取数时间戳, [(名称,涨跌幅,涨跌家数,领涨股)])}
_板缓存秒 = 300            # 板块列表 TTL（一天内几乎不变，避免每次提问都翻 9 页）


def _板块_腾讯(类型: str):
    """返回 [(名称, 涨跌幅, 涨/跌家数, 领涨股)] 或 None（已按涨跌幅降序）。

    ⭐ v5-10：**翻页取全**。腾讯单次 `count` 上限 100（≥500 报"参数错误"），
       但返回体里有 `total` → 按 `offset` 翻页可拿全。
       实测：行业 **31** 个（申万一级）/ 概念 **805** 个。
       ⚠️ 以前只取前 100 → "商业航天"这类排在后面的概念**永远查不到**，
       这正是覆盖复评里"板块类只有 20%"的主因（不是这类需求做不了）。
    加 300s TTL 缓存：板块一天内几乎不变，避免每次提问都发 ~9 个请求。
    """
    import time as _t
    bt = _腾讯板.get(类型)
    if not bt:
        return None
    命中 = _板缓存.get(类型)
    if 命中 and (_t.time() - 命中[0]) < _板缓存秒:
        return 命中[1]
    出, total, offset = [], None, 0
    for _ in range(15):                      # 上限 15 页 = 1500 个，够覆盖当前全量
        url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
               f"board_type={bt}&sort_type=price&direct=down&offset={offset}&count=100")
        文 = _取文本(url, 来源页="https://stockapp.finance.qq.com/")
        if not 文:
            break
        try:
            d = json.loads(文).get("data") or {}
            列 = d.get("rank_list") or []
            if total is None:
                total = d.get("total")
        except Exception:
            break
        if not 列:
            break
        for x in 列:
            领 = x.get("lzg") or {}
            出.append((x.get("name"), x.get("zdf"), x.get("zgb"), 领.get("name")))
        offset += 100
        if total and len(出) >= int(total):
            break
    if not 出:
        return None
    # ⚠️ 不依赖服务端排序：**本地按涨跌幅降序**（服务端 sort_type 支持度不确定）
    出.sort(key=lambda t: -float(t[1] or 0))
    _板缓存[类型] = (_t.time(), 出)
    return 出


@tool
def 查板块(类型: str = "行业", 关键词: str = "") -> str:
    """查**板块行情**（行业板块 / 概念板块）的涨跌幅排行，可只看命中关键词的板块。

    何时该用：用户问"哪个板块涨得最好""证券板块表现如何""XX概念怎么样""板块龙头"。
    何时不该用：问某只个股（用 查实时行情）。

    参数：
      - 类型：行业（默认）或 概念。
      - 关键词：可选，做**本地过滤**（如 "证券"、"人工智能"）；留空则给涨跌幅前 15。

    返回板块名称、涨跌幅、涨跌家数、领涨股，带数据源与时点。
    """
    类型 = (类型 or "行业").strip()
    fs = {"行业": "m:90+t:2", "概念": "m:90+t:3"}.get(类型)
    if not fs:
        return f"不支持的板块类型「{类型}」。可选：行业 / 概念"
    关键词 = (关键词 or "").strip()

    # ① 首选东财 clist（pz 放大到 500：行业 ~86 + 概念 ~400，一次取全）
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           f"pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}"
           "&fields=f12,f14,f3,f104,f105,f128")
    j = _东财(url, 来源页="https://quote.eastmoney.com/center/boardlist.html")
    diff = ((j or {}).get("data") or {}).get("diff") or []
    if diff:
        if 关键词:
            diff = [x for x in diff if 关键词 in (x.get("f14") or "")]
            if not diff:
                return f"{类型}板块里没有名称含「{关键词}」的板块（已检索全部 {类型}板块）。请换关键词，或留空看排行。"
        else:
            diff = diff[:15]
        行 = [f"【{类型}板块】{('匹配「' + 关键词 + '」') if 关键词 else '涨跌幅前 15'} {_时点横幅()}",
             "数据源：东方财富 clist"]
        for x in diff:
            行.append(f"· {x.get('f14')}（{x.get('f12')}） 涨跌 {x.get('f3')}%  ｜ "
                      f"涨 {x.get('f104')} / 跌 {x.get('f105')} 家 ｜ "
                      f"领涨 {x.get('f128') or '—'}")
        return "\n".join(行)

    # ② 东财挂了 → 换腾讯板块（⭐ v5-10：翻页取全，行业 31 / 概念 805）
    备 = _板块_腾讯(类型)
    if not 备:
        return f"没取到{类型}板块行情（东财与腾讯两个源都没响应）—— 请稍后重试。"
    if 关键词:
        备 = [t for t in 备 if 关键词 in (t[0] or "")]
        if not 备:
            return (f"{类型}板块里没有名称含「{关键词}」的板块（备用源已翻页取全，非前 100 截断）"
                    f"—— 请换关键词或稍后重试东财源。")
    else:
        备 = 备[:15]
    行 = [f"【{类型}板块】{('匹配「' + 关键词 + '」') if 关键词 else '涨跌幅前 15'} {_时点横幅()}",
         "数据源：腾讯财经（东财不可用，已切换备用源；已翻页取全部板块后本地过滤）"]
    for 名, 涨, 家数, 领 in 备:
        行.append(f"· {名}  涨跌 {_f(涨)}%  ｜ 涨/跌 {家数 or '—'} 家 ｜ 领涨 {领 or '—'}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ⑤ K线 + 本地算指标（对应"技术指标/K线与均线"17 条）
#    源：腾讯 web.ifzq.gtimg.cn fqkline（前复权日线）
#    ⚠️ 腾讯不打 push2his 那条被风控的路；实测 5/5 通。
#    实测行格式：[日期, 开, 收, 高, 低, 成交量]（注意是 开-收-高-低，不是 OHLC！）
#    ⭐ 这是"派生指标下沉"的第二次实践：均线/MACD/RSI 由本工具**算好**再给模型，
#       模型只负责转述，不再自己拿收盘价去加加减减（v1 讲过的"能力层护栏"）。
# ══════════════════════════════════════════════════════════════
def _ema(数列, n):
    k, e = 2 / (n + 1), None
    out = []
    for v in 数列:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(收盘, n=14):
    if len(收盘) <= n:
        return None
    涨 = [max(收盘[i] - 收盘[i - 1], 0) for i in range(1, len(收盘))]
    跌 = [max(收盘[i - 1] - 收盘[i], 0) for i in range(1, len(收盘))]
    均涨 = sum(涨[:n]) / n
    均跌 = sum(跌[:n]) / n
    for i in range(n, len(涨)):
        均涨 = (均涨 * (n - 1) + 涨[i]) / n
        均跌 = (均跌 * (n - 1) + 跌[i]) / n
    if 均跌 == 0:
        return 100.0
    return 100 - 100 / (1 + 均涨 / 均跌)


@tool
def 查K线(标的: str, 天数: int = 60) -> str:
    """查某只 A 股的**日 K 线 + 技术指标**（均线 MA5/10/20/60、MACD、RSI14、是否多头排列）。

    何时该用：用户问"走势怎么样""均线""MACD""RSI/超买超卖""多头排列""K线形态""支撑压力"
    这类**技术面**问题。
    何时不该用：问今天的价格（用 查实时行情，更快）；问财务（用 查财务指标）。

    参数：
      - 标的：公司名 / wind_code / 6 位代码。
      - 天数：取最近多少根日线来算指标（默认 60，建议 30–250）。

    返回最近 5 个交易日 OHLC + 最新均线/MACD/RSI，带数据源与最新交易日。
    ⚠️ 指标是**本工具按前复权收盘价算的**，不是接口直接给的；答案里要标明"按收盘价计算"。
    """
    代码, 提示 = _认标的(标的)
    if not 代码:
        return 提示
    天数 = max(20, min(int(天数 or 60), 250))
    市场 = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[代码.split(".")[1]]
    信 = f"{市场}{代码.split('.')[0]}"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={信},day,,,{天数},qfq")
    文 = _取文本(url, 编码="utf-8", 来源页="https://gu.qq.com/")
    if not 文:
        return f"没取到 {标的}（{代码}）的 K 线 —— 接口可能抽风，请稍后重试。"
    try:
        d = (json.loads(文).get("data") or {}).get(信) or {}
        bars = d.get("qfqday") or d.get("day") or []
    except Exception:
        return f"{标的} 的 K 线返回无法解析。"
    if len(bars) < 20:
        return f"{标的} 只拿到 {len(bars)} 根日线，不足以算指标。"
    # 腾讯行：[日期, 开, 收, 高, 低, 量]
    日期 = [b[0] for b in bars]
    收盘 = [float(b[2]) for b in bars]
    均 = lambda n: (sum(收盘[-n:]) / n) if len(收盘) >= n else None
    ma5, ma10, ma20, ma60 = 均(5), 均(10), 均(20), 均(60)
    dif = [a - b for a, b in zip(_ema(收盘, 12), _ema(收盘, 26))]
    去 = _ema(dif, 9)
    macd = 2 * (dif[-1] - 去[-1])
    rsi = _rsi(收盘, 14)
    排列 = ("多头排列（MA5>MA10>MA20）" if (ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
            else "空头排列（MA5<MA10<MA20）" if (ma5 and ma10 and ma20 and ma5 < ma10 < ma20)
            else "均线纠缠（无明确排列）")
    行 = [f"【日K与技术指标】{标的}（{代码}） 最新交易日 {日期[-1]}",
         "数据源：腾讯财经（前复权日线）｜ 指标为按收盘价本地计算"]
    行.append("最近 5 个交易日（日期 开/收/高/低）：")
    for b in bars[-5:]:
        行.append(f"　{b[0]}  {b[1]}/{b[2]}/{b[3]}/{b[4]}")
    行.append(f"收盘 {收盘[-1]} ｜ MA5 {ma5:.2f} ｜ MA10 {ma10:.2f} ｜ "
              f"MA20 {ma20:.2f} ｜ " + (f"MA60 {ma60:.2f}" if ma60 else "MA60 样本不足"))
    行.append(f"MACD 柱 {macd:.3f}（DIF {dif[-1]:.3f} / DEA {去[-1]:.3f}） ｜ "
              f"RSI14 {rsi:.1f}" + ("（>70 偏超买）" if rsi and rsi > 70
                                    else "（<30 偏超卖）" if rsi and rsi < 30 else ""))
    行.append(f"均线形态：{排列}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ⑥⑦⑧ datacenter 三兄弟（龙虎榜 / 大宗交易 / 十大流通股东）
#    源：datacenter-web.eastmoney.com（同一套 URL 形状，换 reportName 与 filter）
#    实测均 5/5 通。
# ══════════════════════════════════════════════════════════════
def _datacenter(报表: str, 参数: dict) -> list | None:
    """datacenter 通用取数：拼 query → 取 result.data。失败返回 None。"""
    from urllib.parse import urlencode
    参数 = {"columns": "ALL", "pageSize": 20, "pageNumber": 1,
           "sortColumns": "TRADE_DATE", "sortTypes": -1, **参数}
    参数["reportName"] = 报表
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urlencode(参数, safe="()=*%\"")
    j = _东财(url, 来源页="https://data.eastmoney.com/")
    if not j or not j.get("success"):
        return None
    return ((j.get("result") or {}).get("data")) or []


@tool
def 查龙虎榜(标的: str = "", 天数: int = 1, 日期: str = "") -> str:
    """查**龙虎榜**：最近上榜个股（或指定个股的上榜记录），含买卖额与净额。

    何时该用：用户问"龙虎榜""哪些股票上榜""机构买入""游资席位"，或问**某一天**的龙虎榜。

    参数：
      - 标的：可选，填了就只查这只股票的上榜记录（公司名 / wind_code / 6 位代码）。
      - 天数：取最近几个交易日（默认 1）。**给了日期时忽略此参数**。
      - 日期：可选。形如 "2026-03-19"；给了就只看这一天的龙虎榜。

    返回上榜名单（代码、名称、涨跌幅、换手、龙虎榜净额、上榜原因）。
    """
    过滤 = []
    if 标的.strip():
        代码, 提示 = _认标的(标的)
        if not 代码:
            return 提示
        过滤.append(f'(SECUCODE="{代码}")')
    日 = ""
    if 日期.strip():
        日 = _规范日期(日期)
        if not 日:
            return f"日期「{日期}」看不懂。请用 2026-03-19 这种格式。"
        过滤.append(f"(TRADE_DATE='{日}')")
        天数 = 99                     # 指定日期时不做"最近 N 日"截断
    参 = {"pageSize": 30}
    if 过滤:
        参["filter"] = "".join(过滤)
    j = _datacenter("RPT_DAILYBILLBOARD_DETAILSNEW", 参)
    if not j:
        return "没取到龙虎榜数据 —— 接口可能抽风，请稍后重试，不要编造上榜名单。"
    范围 = f"{日} 当天" if 日 else f"最近 {天数} 个交易日"
    行 = [f"【龙虎榜】{'全部个股' if not 标的 else 标的} {范围}（取前 "
         f"{min(len(j), 15)} 条）", "数据源：东方财富 datacenter"]
    日期集 = sorted({(x.get("TRADE_DATE") or "")[:10] for x in j}, reverse=True)[:天数]
    已列 = 0
    for x in j:
        if 已列 >= 15:                       # ⚠️ 表头写"取前 15 条"，循环也要真的截断（否则会吐满 30 行）
            break
        d = (x.get("TRADE_DATE") or "")[:10]
        if d not in 日期集:
            continue
        行.append(f"· {d} {x.get('SECURITY_NAME_ABBR')}（{x.get('SECURITY_CODE')}） "
                  f"涨跌 {_f(x.get('CHANGE_RATE'))}% ｜ 换手 {_f(x.get('TURNOVERRATE'))}% ｜ "
                  f"龙虎榜净额 {_亿(x.get('BILLBOARD_NET_AMT'))} ｜ {x.get('EXPLANATION') or ''}")
        已列 += 1
    return "\n".join(行) if len(行) > 2 else f"{范围}没有匹配的龙虎榜记录。"


@tool
def 查大宗交易(标的: str = "", 天数: int = 3, 日期: str = "") -> str:
    """查**大宗交易**：最近的大宗成交记录（成交价、折溢价、成交量、买方/卖方营业部）。

    何时该用：用户问"大宗交易""折价成交""谁在大宗接货/出货"，或问**某一天**的大宗交易。

    参数：
      - 标的：可选，填了只查这只股票。
      - 天数：最近几个自然日（默认 3）。**给了日期时忽略此参数**。
      - 日期：可选。形如 "2026-03-19"，只看这一天的成交。

    返回大宗成交明细（日期、股票、成交价、折溢价率、成交额、买卖方）。
    """
    过滤 = []
    if 标的.strip():
        代码, 提示 = _认标的(标的)
        if not 代码:
            return 提示
        过滤.append(f'(SECUCODE="{代码}")')
    日 = ""
    if 日期.strip():
        日 = _规范日期(日期)
        if not 日:
            return f"日期「{日期}」看不懂。请用 2026-03-19 这种格式。"
        过滤.append(f"(TRADE_DATE='{日}')")
    参 = {"pageSize": 30}
    if 过滤:
        参["filter"] = "".join(过滤)
    j = _datacenter("RPT_DATA_BLOCKTRADE", 参)
    if not j:
        return "没取到大宗交易数据 —— 接口可能抽风，请稍后重试。"
    范围 = f"{日} 当天" if 日 else "最近记录"
    行 = [f"【大宗交易】{'全部个股' if not 标的 else 标的} {范围}（取前 {min(len(j), 15)} 条）",
         "数据源：东方财富 datacenter"]
    for x in j[:15]:
        行.append(f"· {(x.get('TRADE_DATE') or '')[:10]} {x.get('SECURITY_NAME_ABBR')}"
                  f"（{x.get('SECURITY_CODE')}） 成交价 {_f(x.get('DEAL_PRICE'))} ｜ "
                  f"折溢 {_f(x.get('PREMIUM_RATIO'))}% ｜ 成交量 {x.get('DEAL_VOLUME')} 股 ｜ "
                  f"成交额 {_亿(x.get('DEAL_AMT'))}")
    return "\n".join(行)


@tool
def 查股东(标的: str) -> str:
    """查某只 A 股的**十大流通股东**（股东名称、持股数、占流通股比例、变动）。

    何时该用：用户问"股东是谁""持股比例""谁在增持/减持""十大流通股东""基金持仓"。

    参数：标的 —— 公司名 / wind_code / 6 位代码。

    返回最新报告期的十大流通股东明细，带报告期与数据源。
    """
    代码, 提示 = _认标的(标的)
    if not 代码:
        return 提示
    j = _datacenter("RPT_F10_EH_FREEHOLDERS",
                    {"pageSize": 20, "sortColumns": "END_DATE,HOLDER_RANK",
                     "sortTypes": "-1,1", "filter": f'(SECUCODE="{代码}")'})
    # ⚠️ 实测坑：这个报表**一次返回多个报告期**的股东（每个报告期都排 1..10 名）。
    #    若只按 HOLDER_RANK 排序分页，前 20 条会全是"各期的第 1 名"，筛完最新期只剩 1 行。
    #    → 正确做法：**按 (报告期降序, 名次升序) 双列排序**，一次就拿到最新期的完整 1..10 名。
    if not j:
        return f"没取到 {标的} 的股东数据 —— 接口可能抽风，或该标的无此表，请稍后重试。"
    最新期 = j[0].get("END_DATE") or ""
    j = [x for x in j if (x.get("END_DATE") or "") == 最新期]
    j.sort(key=lambda x: x.get("HOLDER_RANK") or 99)
    j = j[:10]
    期 = (j[0].get("REPORT_DATE_NAME") or 最新期[:10])
    行 = [f"【十大流通股东】{j[0].get('SECURITY_NAME_ABBR') or 标的}（{代码}） 报告期 {期}",
         "数据源：东方财富 datacenter"]
    for x in j:
        行.append(f"{x.get('HOLDER_RANK')}. {x.get('HOLDER_NAME')} ｜ 持股 "
                  f"{_亿(x.get('HOLD_NUM'))}股 ｜ 占流通 {_f(x.get('FREE_HOLDNUM_RATIO'))}% ｜ "
                  f"{x.get('HOLD_NUM_CHANGE') or ''}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# ⑨ 研报 / ⑩ 快讯（"资讯/研报" 66 条）
#    ⚠️ 两个都是"给不出唯一答案"的领域 —— 工具只负责**把最近的事实捞回来**，
#       要不要下结论由护栏管（v5 第 5 讲的 C 通道）。
# ══════════════════════════════════════════════════════════════
@tool
def 查研报(标的: str = "", 数量: int = 5) -> str:
    """查**券商研报**（近期研报标题、机构、评级、发布日期）。

    何时该用：用户问"最新研报""机构怎么看""评级""目标价"。
    何时不该用：问实时行情或财务数字。

    参数：
      - 标的：可选，填了只看这只股票的研报（公司名 / wind_code / 6 位代码）。
      - 数量：返回几条（默认 5）。

    返回研报列表。⚠️ 研报是**机构观点**，不是事实；引用时要写明是哪家机构、哪天发的。
    """
    参 = {"pageSize": max(1, min(int(数量 or 5), 20)), "pageNo": 1, "qType": 0,
          "beginTime": "2026-01-01", "endTime": date.today().strftime("%Y-%m-%d"),
          "industry": "*"}
    if 标的.strip():
        代码, 提示 = _认标的(标的)
        if not 代码:
            return 提示
        参["code"] = 代码.split(".")[0]
    else:
        参["code"] = "*"
    from urllib.parse import urlencode
    url = "https://reportapi.eastmoney.com/report/list?" + urlencode(参)
    j = _东财(url, 来源页="https://data.eastmoney.com/report/")
    data = (j or {}).get("data") or []
    if not data:
        return f"没取到{'「' + 标的 + '」的' if 标的 else ''}研报 —— 接口可能抽风，请稍后重试。"
    行 = [f"【券商研报】{'个股 ' + 标的 if 标的 else '全市场最新'}（就近取 {len(data)} 条）",
         "数据源：东方财富 reportapi ｜ ⚠️ 以下为机构观点，非事实"]
    for x in data:
        行.append(f"· {(x.get('publishDate') or '')[:10]} 【{x.get('orgSName')}】"
                  f"{x.get('stockName') or ''} {x.get('emRatingName') or ''}："
                  f"{(x.get('title') or '')[:60]}")
    return "\n".join(行)


@tool
def 查快讯(数量: int = 8) -> str:
    """查**财经快讯**（东财 7×24 实时快讯，含大盘快照与要闻）。

    何时该用：用户问"最新消息""今天有什么新闻""市场动态""有什么大事"。

    参数：数量 —— 返回几条（默认 8）。

    返回快讯（时间 + 标题/摘要），带刷新时点。
    """
    数量 = max(1, min(int(数量 or 8), 20))
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList?"
           f"client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={数量}&req_trace=1")
    j = _东财(url, 来源页="https://kuaixun.eastmoney.com/")
    列 = (((j or {}).get("data") or {}).get("fastNewsList")) or []
    if not 列:
        return "没取到快讯 —— 接口可能抽风，请稍后重试。"
    行 = [f"【财经快讯】最新 {len(列)} 条 {_时点横幅()}", "数据源：东方财富快讯"]
    for x in 列:
        行.append(f"· {(x.get('showTime') or x.get('time') or '')[:19]} "
                  f"{(x.get('title') or x.get('summary') or '')[:70]}")
    return "\n".join(行)


# ══════════════════════════════════════════════════════════════
# 供 graph.py 一次性导入
# ══════════════════════════════════════════════════════════════
市场工具 = [查资金流, 查排行, 查涨停池, 查板块, 查K线,
          查龙虎榜, 查大宗交易, 查股东, 查研报, 查快讯]
