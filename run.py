# run.py —— 把图跑起来，用 stream 看每一步（第 6、12 课：观察超步/节点）。
#
# 用法：
#   python run.py                                   # 交互式输入
#   python run.py "宝钢股份 2025 年营业总收入是多少？"   # 直接给问题（方便快速测/脚本）
#
# 重点不是"跑通"，是"看清每一步图在干嘛"。
# v2 第 2 讲起：图会"人工介入"（interrupt），所以这里换成共用函数 cli.跑一轮 来跑。

import sys
import uuid

from langgraph.checkpoint.memory import InMemorySaver

from cli import 跑一轮
from graph import build_graph


def main() -> None:
    # v2：① interrupt 必须有 checkpointer 才跑得起来；② 每次进程开一个独立会话（thread_id 随机）
    app = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "cli-" + uuid.uuid4().hex[:6]}}

    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:]).strip()
    else:
        q = input("你的问题（例：宝钢股份 2025 年的营业总收入是多少？）：\n> ").strip()
    if not q:
        print("没输入，退出。")
        return

    print("=" * 50)
    print("stream_mode='updates' —— 每个节点写了什么：")
    # 第 12 课：updates = 每节点一条增量（{节点名: 更新}）。
    # 对比着看：换成 stream_mode='values' 则是每超步一张全量快照，条数 = 超步数 + 1。
    最终 = 跑一轮(app, config, {"messages": [("user", q)]}, 展示每步=True)

    print("=" * 50)
    print("最终回答：")
    if 最终 is not None:
        最终.pretty_print()
    else:
        print("（没有产出回答）")


if __name__ == "__main__":
    main()
