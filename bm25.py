# bm25.py —— v3 第 3 讲：BM25 关键词检索（中文用"字符 bigram"分词，不依赖 jieba）。
#
# 【为什么需要它】
#   向量检索管"语义像不像"（把"营业总收入"和"营业收入"当成近义）；
#   但问"宝钢2023年营业总收入"时，"2023""业绩快报"这些**精确的实词/数字**才是最硬的线索，
#   而向量对这种"字面精确性"不敏感（第 1 讲实测：5 个相似度挤在 0.82，区分不开）。
#   BM25 正好补这一刀：**它算的是"词命中程度"，专治精确实词和数字**。
#
# 【BM25 是什么】
#   Okapi BM25 = 经典的词袋检索打分。对每个查询词 t，在文档 d 上的贡献是
#       IDF(t) · tf · (k1+1) / ( tf + k1·(1 - b + b·len(d)/avg_len) )
#   直觉（三句话）：
#     1. IDF：**越少见的词越值钱**（"营业总收入"比"公司"重要）；
#     2. tf 的饱和：一个词出现 10 次不该比出现 1 次重要 10 倍 → 分子分母让它"递减增长"；
#     3. 长度归一化(b)：长文档天然更容易命中词，要**除以长度**打个折，不然长的永远占便宜。
#
# 【中文怎么分词】
#   中文没有空格，标准做法是上分词器（jieba）。但这里用**字符 bigram**代替：
#     "业绩快报" → 业-绩, 绩-快, 快-报
#   效果接近、零依赖，而且对数字/代码天然友好（"2023"整体保留）。

import math
import re
from collections import Counter

_英文数字 = re.compile(r"[a-z0-9]+", re.I)
_汉字串 = re.compile(r"[\u4e00-\u9fff]+")


def 分词(文本: str) -> list[str]:
    """中文 → 字符 bigram；英文/数字 → 整串（小写）。"""
    词: list[str] = []
    文本 = 文本.lower()
    for m in _英文数字.finditer(文本):
        词.append(m.group())
    for m in _汉字串.finditer(文本):
        s = m.group()
        if len(s) == 1:
            词.append(s)
        else:
            词.extend(s[i:i + 2] for i in range(len(s) - 1))
    return 词


class BM25:
    def __init__(self, 文档们: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.文档数 = len(文档们)
        self.词频 = [Counter(分词(d)) for d in 文档们]
        self.长度 = [sum(c.values()) for c in self.词频]
        self.平均长度 = (sum(self.长度) / self.文档数) if self.文档数 else 1.0
        # 文档频：每个词出现在多少篇文档里（算 IDF 用）
        df: Counter = Counter()
        for c in self.词频:
            df.update(c.keys())
        self.文档频 = df

    def _idf(self, 词: str) -> float:
        df = self.文档频.get(词, 0)
        # +0.5 平滑，+1 保证 IDF 非负
        return math.log((self.文档数 - df + 0.5) / (df + 0.5) + 1.0)

    def 打分(self, 查询: str) -> list[float]:
        分 = [0.0] * self.文档数
        for 词 in set(分词(查询)):
            if self.文档频.get(词, 0) == 0:
                continue
            idf = self._idf(词)
            for i, tf in enumerate(self.词频):
                f = tf.get(词, 0)
                if not f:
                    continue
                长度比 = self.长度[i] / self.平均长度
                分[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * 长度比))
        return 分

    def 检索(self, 查询: str, 条数: int = 5) -> list[tuple[int, float]]:
        """返回 [(库内下标, BM25 分)]，按分数降序（分数为 0 的也返回，调用方自己截断）。"""
        分 = self.打分(查询)
        排名 = sorted(range(self.文档数), key=lambda i: -分[i])[:条数]
        return [(i, 分[i]) for i in 排名]


if __name__ == "__main__":
    # 自测：明显该排第一的那条，分数应该远高于其他
    语料 = [
        "2023 年度业绩快报公告 营业总收入 346,926 归属于上市公司股东的净利润 12,007",
        "2024 年年度报告 母公司利润表 营业收入 118,319,040,327.56",
        "关于终止分拆所属子公司至创业板上市的公告",
    ]
    bm = BM25(语料)
    for i, s in bm.检索("宝钢2023年度业绩快报的营业总收入", 条数=3):
        print(f"[{i}] {s:.3f}  {语料[i][:50]}")
