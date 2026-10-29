# stock_names.py —— C 路线：全市场「中文名 → wind_code」解析层。
#
# 【它在架构里的位置】
#   这是 tools.py 里那张 7 行小字典 `公司映射` 的**全市场版**，并且被抽成独立模块，
#   让 `tools.py`（查财务指标）和 `market_tool.py`（所有实时接口工具）**共用同一套名字解析**。
#   —— 否则每个工具各写一份，早晚不一致（这就是"翻译层要唯一"的道理）。
#
# 【三种输入都要认】
#   ① 中文名   "宝钢股份"     → 查 stock_names.json（全市场 ~5900 条）
#   ② wind_code "600019.SH"   → 原样返回
#   ③ 裸代码    "600019"      → 按前缀补交易所（问"688001卖盘"这种就得靠它）
#   认不出来返回 None —— 让工具走"如实说"兜底，**不要瞎猜一个代码去查**。
#
# 【为什么用 JSON 而不是查库】
#   名字→代码是**低频变动的静态映射**，随代码走比随 MySQL 走更可靠：
#   MySQL 在这台机器上经常没起（M 盘不可见就起不来），而名单是"启动即用"。

import json
import os
import re

_路径 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_names.json")

# 内置兜底：即使 stock_names.json 丢了，这 7 家（原 tools.py 手工表）也还能用。
_内置 = {
    "宝钢股份": "600019.SH", "中国石油": "601857.SH", "宁德时代": "300750.SZ",
    "贵州茅台": "600519.SH", "中国石化": "600028.SH", "中国神华": "601088.SH",
    "中兴通讯": "000063.SZ",
}

_代码格式 = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_裸代码 = re.compile(r"^\d{6}$")

_缓存: dict | None = None


def 交易所(代码: str) -> str | None:
    """按**代码前缀**判交易所（别用东财 f13 —— 它把北交所混进深市）。"""
    if 代码.startswith(("60", "68", "90")):
        return "SH"
    if 代码.startswith(("00", "30", "20")):
        return "SZ"
    if 代码.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    return None


def 名称表() -> dict:
    """懒加载全市场名单；文件缺失/损坏时退回内置 7 家，绝不抛异常。"""
    global _缓存
    if _缓存 is None:
        try:
            with open(_路径, encoding="utf-8") as f:
                _缓存 = json.load(f)
            if not isinstance(_缓存, dict) or not _缓存:
                raise ValueError("空名单")
        except Exception:
            _缓存 = dict(_内置)
        # 内置的永远在里面（防止名单里漏了这几家）
        for k, v in _内置.items():
            _缓存.setdefault(k, v)
        # 再补一份"规范化别名"（旧名单里可能存着带空格/全角的名字）
        for k in list(_缓存):
            _缓存.setdefault(规范(k), _缓存[k])
    return _缓存


def 规范(名: str) -> str:
    """统一名字写法：去空格（含全角）+ 全角字母数字转半角。
    实测腾讯把「万科A」写成「万 科Ａ」—— 两边都规范化才不会擦肩而过。"""
    名 = (名 or "").strip().replace(" ", "").replace("\u3000", "")
    return "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in 名)


def 解析公司(名: str) -> str | None:
    """中文名 / wind_code / 裸代码 → wind_code。认不出返回 None。"""
    名 = 规范(名)
    if not 名:
        return None
    if _代码格式.match(名.upper()):
        return 名.upper()
    if _裸代码.match(名):
        交 = 交易所(名)
        return f"{名}.{交}" if 交 else None
    表 = 名称表()
    if 名 in 表:
        return 表[名]
    # 轻量模糊：用户常少写"股份/集团"后缀（"宝钢" → "宝钢股份"）
    for 后缀 in ("股份", "集团", "控股"):
        if 名 + 后缀 in 表:
            return 表[名 + 后缀]
    return None


def 已知(名: str) -> bool:
    """这个标的我们认不认得（决定要不要停下来问用户要代码）。"""
    return 解析公司(名) is not None


if __name__ == "__main__":
    for s in ["宝钢股份", "600019.SH", "600019", "宝钢", "宁德时代", "寒武纪",
              "中际旭创", "不存在公司", "688001"]:
        print(f"{s:12} → {解析公司(s)}")
    print("名单规模：", len(名称表()))
