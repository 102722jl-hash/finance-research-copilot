# config.py —— 所有"外部连接"集中在这里。
#
# ✍️ 你只需要填环境变量，不用改这里的逻辑。
# 框架无关的一点：密钥/密码永远走环境变量，不进代码（和你的 MYSQL_PWD 约定一致）。

import json
import os


def _load_env(path: str | None = None) -> None:
    """极简 .env 读取（不依赖 python-dotenv 包）。
    文件格式：KEY=VALUE，每行一个，# 开头是注释。

    - 默认读"本文件同目录"下的 .env（不依赖你从哪个目录运行）。
    - 只写入**非空**值：否则 .env 里那行空的 `DB_PASSWORD=` 会把
      环境变量里的 MYSQL_PWD 顶掉，导致连库失败。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if v:                       # 空值不写入，交给下面用 or 回退
                os.environ.setdefault(k, v)


_load_env()


# ── LLM（OpenAI 兼容端点）──────────────────────────────────
# ✍️ 把下面换成你自己的端点。DeepSeek / 你现有 OpenAI 兼容端点都行。
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")          # ✍️ 在 .env 里填，别硬编码
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# ── 模型侧提速：关思考（v5-6 实测新增）────────────────────────
# 起因：v5-5 里 20~40s 的请求耗时，原以为是"工具慢"。实测把工具排除后（检索() 整条仅 211ms），
#   真凶是**推理模型的隐藏 thinking**：耗时几乎 = 输出token ÷ 150，
#   而"请只回复两个字：OK"也要烧掉 242 个输出 token。
#
# 实测（同一问题「宝钢股份的基本面怎么样？请展开分析」）：
#   不关思考          32.7s / 4125 输出 token
#   reasoning_effort=none   8.6s / 1161 输出 token   ← 约 4 倍
#   chat_template_kwargs={"enable_thinking": False}   7.7s（另一种传法，同理有效）
#   enable_thinking=False 直传   22.3s（❌ 该网关不认）
#   reasoning_effort=low          41.9s（❌ 反而更慢）
#   提示词加 /no_think            28.4s（❌ 基本无效）
#
# ⚠️ 为什么做成开关而不是写死：关思考**会改变答案组织方式**（实测答案字数 2891 → 2096），
#    质量影响必须用评测集验（见 v5 第 6 讲）。留个 env 开关方便 A/B。
#    取值："none" 关思考 ｜ "" 不干预（用模型默认）
#    ⚠️ 默认改成空串：因为 .env 读取逻辑是"空值不覆盖默认"，若默认是 "none"，
#       换厂商时留空也关不掉。现在**要关思考必须显式写** `LLM_REASONING_EFFORT=none`。
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "")

# ── 通用 extra_body（JSON 字符串，原样透传给模型）──────────────
# 起因：不同厂商传"非标准参数"的键名五花八门 —— 有的认 reasoning_effort，
#   火山方舟的 doubao-seed-1.6 则要 {"thinking": {"type": "disabled"}}。
#   → 与其给每家写一个分支，不如开一个"原样透传"的口子：这里填 JSON，就并进 extra_body。
# 例（火山方舟关思考）：LLM_EXTRA_BODY={"thinking": {"type": "disabled"}}
LLM_EXTRA_BODY: dict = {}
try:
    _raw_extra = os.getenv("LLM_EXTRA_BODY", "").strip()
    if _raw_extra:
        LLM_EXTRA_BODY = json.loads(_raw_extra)
except Exception:
    LLM_EXTRA_BODY = {}


def llm_kwargs() -> dict:
    """所有 ChatOpenAI 共用的构造参数（含 extra_body 透传）。

    抽出来是为了**只改一处**：以前 graph.py / guard.py 各建各的模型，
    加个新参数要改 N 个地方，容易漏。
    """
    参数 = {
        "model": LLM_MODEL,
        "api_key": LLM_API_KEY,
        "base_url": LLM_BASE_URL,
        "temperature": 0,
    }
    extra = dict(LLM_EXTRA_BODY)
    if LLM_REASONING_EFFORT:                      # 兼容老网关的写法
        extra["reasoning_effort"] = LLM_REASONING_EFFORT
    if extra:
        参数["extra_body"] = extra
    return 参数


# ── 向量 / 重排端点（可与上面的聊天端点分开；不填就沿用 LLM_*）──────
# 为什么拆开：本项目**不只用网关"聊天"**，还用同一个端点做另外两件事 ——
#   ① embedding（bge-large-zh，1024 维）→ 给"指标字典语义检索 / 公告检索"用；
#   ② rerank（bge-reranker-large）→ 给混合检索的精排用。
# 一旦换聊天端点（比如从内网网关换成公网厂商），这两条链路也得跟着换，
# 而且**不同厂商模型名不一样**（例：硅基流动叫 BAAI/bge-large-zh-v1.5）。
#   → 三个都拆成独立变量：默认沿用 LLM_*，需要时在 .env 里单独覆盖。
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "") or LLM_BASE_URL
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "") or LLM_API_KEY
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-large-zh")

RERANK_BASE_URL = os.getenv("RERANK_BASE_URL", "") or LLM_BASE_URL
RERANK_API_KEY = os.getenv("RERANK_API_KEY", "") or LLM_API_KEY
RERANK_MODEL = os.getenv("RERANK_MODEL", "bge-reranker-large")


# ── MySQL（本机 workspace 库，A 股数据）─────────────────────
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_NAME = os.getenv("DB_NAME", "workspace")
# 密码：DB_PASSWORD 非空才用，否则回退到你习惯的 MYSQL_PWD（空字符串不算数）
DB_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PWD", "")
