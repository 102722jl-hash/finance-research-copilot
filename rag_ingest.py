# rag_ingest.py —— v3 第 1 讲：把公告 PDF 灌进向量库（离线建库）。
#
# 链路：PDF → 抽文本（带页码）→ 切块 → embedding（bge-large-zh）→ 存进向量库
#
# 用法：
#   "...python.exe" rag_ingest.py                      # 默认小样本：600019.SH（宝钢 10 份）
#   "...python.exe" rag_ingest.py 600019.SH 601857.SH   # 指定多只
#
# ✍️ 动手区在下面的 切块器 参数：chunk_size / chunk_overlap。
#    - 太小：一句被切两半，检索到半句没法用；
#    - 太大：一个块里混进好几件事，向量被"平均"掉，反而搜不准。

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from config import EMBED_API_KEY, EMBED_BASE_URL, EMBED_MODEL
from vector_store import 向量库

语料根 = r"D:\桌面\workspace\codex1\inputs\announcements\v0.2\snapshots"
库存目录 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_store")

初始批量 = 16     # 实测：一次 64 条会把网关后端 GPU 打到 OOM，16 稳
最小批量 = 4


# ══════════════════════════════════════════════════════════════
# 多模态向量模型适配器（火山方舟 doubao-embedding-vision-*）
# ══════════════════════════════════════════════════════════════
# 【为什么不能直接用 langchain 的 OpenAIEmbeddings】
#   实测（2026-10-16 直调方舟）这个模型有三点"不一样"：
#     ① 走 **/embeddings/multimodal**，不是 /embeddings —— 走后者会 400
#        "does not support this api"；
#     ② 请求体必须是 **[{"type":"text","text":"..."}]**；纯字符串数组会 400 解析失败；
#     ③ **不支持批量**：input 里放 N 条 = "一个文档的 N 个部分" → 只回**一个**向量
#        （实测放 3 条文本，返回 1 个 2048 维向量，token 数 ≈ 3 条之和）。
#        ⇒ N 条文本 = N 次请求，所以这里必须自己上并发，否则 1800 条要十几分钟。
#
# 接口对齐：只需实现 embed_documents / embed_query 两个方法 —— 全项目只用这两个
#   （rag_ingest.向量化 走前者；hybrid_search.向量排名 / schema_index 走后者）。
class 多模态嵌入器:
    def __init__(self, 模型: str, base_url: str, api_key: str,
                 维度: int | None = None, 并发: int = 4, 重试: int = 4):
        self.模型 = 模型
        self.端点 = base_url.rstrip("/")
        self.密钥 = api_key
        self.维度 = 维度
        self.并发 = 并发
        self.重试 = 重试

    def _一条(self, 文本: str) -> list[float]:
        body: dict = {"model": self.模型, "input": [{"type": "text", "text": 文本}]}
        if self.维度:
            body["dimensions"] = self.维度
        数据 = json.dumps(body).encode("utf-8")
        上次错: object = None
        for 第次 in range(self.重试):
            req = urllib.request.Request(
                self.端点 + "/embeddings/multimodal", data=数据,
                headers={"Authorization": f"Bearer {self.密钥}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))["data"]["embedding"]
            except urllib.error.HTTPError as e:
                上次错 = e
                可重试 = e.code in (429, 500, 502, 503, 504)
                if 可重试 and 第次 < self.重试 - 1:
                    time.sleep(1.5 * (第次 + 1))
                    continue
                raise
            except Exception as e:                       # 网络抖动/超时
                上次错 = e
                if 第次 < self.重试 - 1:
                    time.sleep(1.5 * (第次 + 1))
                    continue
                raise
        raise RuntimeError(f"embedding 失败：{上次错}")

    def embed_documents(self, 文本们: list[str], chunk_size: int | None = None) -> list[list[float]]:
        with ThreadPoolExecutor(max_workers=self.并发) as 池:
            return list(池.map(self._一条, 文本们))

    def embed_query(self, 文本: str) -> list[float]:
        return self._一条(文本)


def 建embedding():
    """返回嵌入器。**按模型名自动选实现**，换厂商只改 .env：

      · `EMBED_MODEL` 含 "vision"（火山的多模态向量模型）→ 多模态嵌入器；
      · 否则 → 标准 OpenAI 兼容的 OpenAIEmbeddings。

    手动指定风格：`EMBED_STYLE=multimodal|openai`。
    降维：`EMBED_DIMENSIONS=1024`（火山多模态向量支持 512/1024/2048）。
    """
    风格 = os.getenv("EMBED_STYLE") or ("multimodal" if "vision" in EMBED_MODEL.lower() else "openai")
    if 风格 == "multimodal":
        维度 = int(os.getenv("EMBED_DIMENSIONS") or 0) or None
        return 多模态嵌入器(EMBED_MODEL, EMBED_BASE_URL, EMBED_API_KEY, 维度=维度)

    # ⚠️ check_embedding_ctx_length=False：否则 langchain 会用 tiktoken 去"猜"这个模型的
    #    token 上限 —— 它不认识这类模型名，会走上一堆无谓的分片逻辑。直接发原文最稳。
    return OpenAIEmbeddings(
        model=EMBED_MODEL,
        base_url=EMBED_BASE_URL,
        api_key=EMBED_API_KEY,
        check_embedding_ctx_length=False,
        chunk_size=初始批量,
    )


def 向量化(嵌入器, 文本们: list[str]) -> list[list[float]]:
    """分批向量化，失败就"批量减半"重试 —— 生产里必须这么干。

    真实踩到的两个坑（都实测复现过）：
      - 413 batch size 1000 > maximum allowed batch size 64   → 单次批量有上限
      - 424 Backend error: CUDA_ERROR_OUT_OF_MEMORY           → 后端显存不够
    两者都不是"重试一次就好"，而是**必须把批量调小**。
    """
    向量们: list[list[float]] = []
    批量 = 初始批量
    i = 0
    while i < len(文本们):
        一片 = 文本们[i:i + 批量]
        try:
            向量们.extend(嵌入器.embed_documents(一片, chunk_size=批量))
            i += len(一片)
            print(f"   已向量化 {i}/{len(文本们)}（批量 {批量}）")
        except Exception as e:
            if 批量 > 最小批量:
                批量 = max(最小批量, 批量 // 2)
                print(f"   ⚠️ 出错（{type(e).__name__}: {str(e)[:60]}）→ 批量降到 {批量} 重试")
            else:
                raise
    return 向量们


def 列PDF(股票代码: str) -> list[str]:
    路径们 = []
    for sub in ("input", "hidden"):
        d = os.path.join(语料根, 股票代码, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".pdf"):
                    路径们.append(os.path.join(d, f))
    return 路径们


def 抽页(pdf路径: str) -> list[tuple[int, str]]:
    """返回 [(页码, 文本)]；页码从 1 开始（引用时要标出来）。"""
    页 = []
    try:
        for i, page in enumerate(PdfReader(pdf路径).pages, start=1):
            t = (page.extract_text() or "").strip()
            if t:
                页.append((i, t))
    except Exception as e:
        print(f"  ！抽文本失败：{os.path.basename(pdf路径)} → {type(e).__name__}: {e}")
    return 页


def main(股票们: list[str]) -> None:
    切块器 = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],   # 中文优先按句号断
    )

    print("① 抽文本 + 切块…")
    文本们, 记录们 = [], []
    for 代码 in 股票们:
        for pdf in 列PDF(代码):
            文件名 = os.path.basename(pdf)
            for 页码, 文本 in 抽页(pdf):
                for 块 in 切块器.split_text(文本):
                    块 = 块.strip()
                    if len(块) < 20:            # 太短的碎片丢掉
                        continue
                    文本们.append(块)
                    记录们.append({"股票": 代码, "文件": 文件名, "页码": 页码, "文本": 块})
        print(f"   {代码}：累计 {len(文本们)} 块")

    if not 文本们:
        print("没有切出任何块，退出。")
        return

    print(f"② embedding（共 {len(文本们)} 块，模型 bge-large-zh）…")
    向量们 = 向量化(建embedding(), 文本们)

    print("③ 写进向量库…")
    库 = 向量库(库存目录)
    库.打开()                        # 已有就接着往里加
    库.写入(向量们, 记录们)

    print(f"完成：库现在共 {库.条数()} 条；目录 {库存目录}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["600019.SH"])
