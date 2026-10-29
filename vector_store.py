# vector_store.py —— v3 第 1 讲：最小向量库（手搓，只用 numpy）。
#
# 【为什么不用 Chroma】
#   本机装了 chromadb 1.5.9，小集合（3 条）完全正常；但把 1370 条公告块写进去并**重开进程**后，
#   查询报 `Error loading hnsw index`，且段目录里只有 index_metadata.pickle、**索引本体没落盘**。
#   → 这台 Windows + chromadb 的 Rust 绑定在规模上有坑。
#   为了先把 RAG 链路跑通，这里手搓一个最小向量库（40 行），
#   反而更能看清"向量库到底在干嘛"。以后要换 Chroma / FAISS / pgvector，**只改这一层**。
#
# 【向量库的本质】
#   就是一个容器：一堆向量 + 对应的原文 + 元数据。
#   检索 = query 向量和每一个库向量算余弦相似度，取 top-k。
#   没有魔法 —— Chroma/FAISS 只是把这句做得更快（HNSW/IVF 等近似索引）+ 更工程化。

import json
import os

import numpy as np


class 向量库:
    def __init__(self, 目录: str):
        self.目录 = 目录
        self.向量文件 = os.path.join(目录, "vectors.npy")
        self.记录文件 = os.path.join(目录, "docs.json")
        self.向量: np.ndarray | None = None
        self.记录: list[dict] = []

    # ── 打开已有库 ──
    def 打开(self) -> "向量库":
        if os.path.exists(self.向量文件):
            self.向量 = np.load(self.向量文件)
            with open(self.记录文件, "r", encoding="utf-8") as f:
                self.记录 = json.load(f)
        return self

    # ── 追加写入（向量 + 记录一起存）──
    def 写入(self, 向量们: list[list[float]], 记录们: list[dict]) -> None:
        os.makedirs(self.目录, exist_ok=True)
        新向量 = np.asarray(向量们, dtype=np.float32)
        self.向量 = 新向量 if self.向量 is None else np.vstack([self.向量, 新向量])
        self.记录.extend(记录们)
        np.save(self.向量文件, self.向量)
        with open(self.记录文件, "w", encoding="utf-8") as f:
            json.dump(self.记录, f, ensure_ascii=False)

    def 条数(self) -> int:
        return 0 if self.向量 is None else int(self.向量.shape[0])

    # ── 相似度打分（返回每个库向量的余弦相似度，长度 = 库的条数）──
    def 打分(self, 查询向量: list[float]) -> np.ndarray:
        """归一化后点积 = 余弦。返回长度 N 的分数数组。"""
        if self.向量 is None or self.条数() == 0:
            return np.zeros(0, dtype=np.float32)
        q = np.asarray(查询向量, dtype=np.float32)
        库 = self.向量 / (np.linalg.norm(self.向量, axis=1, keepdims=True) + 1e-9)
        v = q / (np.linalg.norm(q) + 1e-9)
        return 库 @ v

    # ── 检索（返回下标版）——v3 第 3 讲新增：混合检索需要"下标"来对齐两路排名 ──
    def 检索下标(self, 查询向量: list[float], 条数: int = 5) -> list[tuple[int, float]]:
        """返回 [(库内下标, 相似度)]，按相似度降序。"""
        分数 = self.打分(查询向量)
        if 分数.size == 0:
            return []
        排名 = np.argsort(-分数)[:条数]
        return [(int(i), float(分数[int(i)])) for i in 排名]

    # ── 检索：余弦相似度 top-k（返回记录）──
    def 检索(self, 查询向量: list[float], 条数: int = 5) -> list[dict]:
        命中 = []
        for i, s in self.检索下标(查询向量, 条数):
            一条 = dict(self.记录[i])
            一条["相似度"] = round(s, 4)
            命中.append(一条)
        return 命中
