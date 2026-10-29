# run_multi.py —— v2 第 1 讲：多轮对话；v2 第 3 讲：跨会话的"用户偏好"。
#
# 用法：
#   "D:/桌面/workspace/study2 - 豆包/langgraph-learning/.venv/Scripts/python.exe" run_multi.py
#   "...python.exe" run_multi.py 我的会话
#
# 两种"记忆"在这里同时演示：
#   会话内记忆 = checkpointer（靠同一个 thread_id）
#   跨会话记忆 = store（靠命名空间，本文件里是 ("偏好","me")）
#
# 斜杠命令：
#   /偏好 <内容>   记住一条长期偏好（换会话也还在）
#   /新会话        开一段全新对话（对话记忆清空，偏好保留）
#   /会话          看当前会话 id、消息条数、已存偏好

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from cli import 跑一轮
from graph import build_graph, 偏好命名空间, 偏好键


def 消息条数(app, config) -> int:
    try:
        return len(app.get_state(config).values.get("messages", []))
    except Exception:
        return 0


def main(thread_id: str) -> None:
    # ✍️ 第 1 讲动手区 ①：把 checkpointer 交给图（也是人工介入的必要条件）。
    # ✍️ 第 3 讲动手区：把 store 交给图 —— 它管"跨会话"的长期记忆。
    store = InMemoryStore()
    app = build_graph(checkpointer=InMemorySaver(), store=store)

    # ✍️ 第 1 讲动手区 ②：thread_id = "会话的名字"，同一个 id 共用一段记忆。
    config = {"configurable": {"thread_id": thread_id}}
    序号 = [1]

    print(f"当前会话「{thread_id}」")
    print("命令：/偏好 <内容> ／ /新会话 ／ /会话 ／ q 退出")
    while True:
        try:
            q = input("你> ").strip()
        except EOFError:
            break
        if q.lower() in ("q", "quit", "exit"):
            break
        if not q:
            continue

        if q.startswith("/偏好"):
            内容 = q[3:].strip()
            if not 内容:
                print("用法：/偏好 金额一律换算成亿元，保留两位小数")
                continue
            store.put(偏好命名空间, 偏好键, 内容)
            print(f"[已记住] {内容}  —— 换个会话也还在")
            continue

        if q == "/新会话":
            序号[0] += 1
            新id = f"{thread_id}-{序号[0]}"
            config = {"configurable": {"thread_id": 新id}}
            print(f"[已开新会话] {新id}：对话记忆清空，但长期偏好保留")
            continue

        if q == "/会话":
            项 = store.get(偏好命名空间, 偏好键)
            print(f"[当前会话] {config['configurable']['thread_id']}"
                  f"｜消息 {消息条数(app, config)} 条"
                  f"｜偏好：{项.value if 项 else '（无）'}")
            continue

        最终 = 跑一轮(app, config, {"messages": [("user", q)]})
        print("agent>", (最终.content or "").strip() if 最终 else "（没有产出回答）")
        print(f"        [本会话累计 {消息条数(app, config)} 条消息]")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-1")
