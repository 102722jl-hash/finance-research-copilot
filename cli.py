# cli.py —— v2 第 2 讲：把"跑一轮（含人工介入）"抽出来，run.py / run_multi.py 共用。
#
# 为什么单独一个文件：一旦图会 interrupt，调用方就得会"接住中断 → 问用户 → resume"。
# 这段逻辑两个入口都要用，复制两份迟早对不上，所以抽这里。

from langchain_core.messages import AIMessage
from langgraph.types import Command


def 跑一轮(app, config, 输入, 展示每步: bool = False) -> AIMessage | None:
    """把一次提问跑到底；中途若图停在 interrupt，就就地问用户，再 resume 回去。

    - app     ：compile 好、且**带 checkpointer** 的图
    - config  ：含 thread_id 的配置（resume 必须落回同一个 thread）
    - 输入    ：首次是 {"messages":[("user", 问题)]}；恢复时换成 Command(resume=答复)
    - 返回    ：最后一条 AI 消息（可能就是回答，也可能是护栏拦截 / 放弃的说明）
    """
    最终 = None
    当前输入 = 输入

    while True:
        中断问题 = None
        for chunk in app.stream(当前输入, stream_mode="updates", config=config):
            if 展示每步:
                print("STEP:", chunk)
            # 图停住了：这一块的特殊 key 就是中断载荷
            if "__interrupt__" in chunk:
                中断问题 = chunk["__interrupt__"][0].value
                continue
            for 节点输出 in chunk.values():
                if not isinstance(节点输出, dict):
                    continue
                for m in 节点输出.get("messages", []) or []:
                    if isinstance(m, AIMessage):
                        最终 = m

        if 中断问题 is None:
            return 最终

        # 有中断 → 问用户，把答复 resume 回去，图从这里接着跑
        print("-" * 50)
        print(f"[需要你确认] {中断问题}")
        答复 = input("> ").strip()
        当前输入 = Command(resume=答复)
