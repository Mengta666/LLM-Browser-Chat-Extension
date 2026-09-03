"""长期记忆子系统的配置常量。

记忆存储对齐 mem0:Qdrant payload 是事实源,SQLite 只做变更审计日志。
所有参数均从 .env 读取,默认值对齐当前 Qwen3-Embedding-8B 部署。
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)


def _load_calibrated_thresholds() -> dict:
    """B6:按 embedding 模型名加载 config/thresholds_<model>.json,替代硬编码魔数。

    找不到文件或字段缺失时不影响启动,退回 env/默认值。cosine 分布不可跨模型迁移
    (arXiv:2310.13994),换 embedding 需重跑 test/eval/calibrate_thresholds.py。
    """
    embed_model = os.environ.get("EMBEDDING_MODEL", "")
    if not embed_model:
        return {}
    safe_name = embed_model.replace("/", "_").replace(":", "_")
    path = Path(__file__).resolve().parents[2] / "config" / f"thresholds_{safe_name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_CAL = _load_calibrated_thresholds()

# Qdrant 连接(复用页面 RAG 的同一实例)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_DISTANCE = os.getenv("QDRANT_DISTANCE", "Cosine")

# 记忆专用 collection(与页面 RAG 的 browser_pages 分开)
MEMORY_COLLECTION = os.getenv("QDRANT_MEMORY_COLLECTION", "agent_memories")

# 向量维度:必须与 embedding 模型输出维度一致,换模型时同步修改 .env 并重建 collection。
MEMORY_VECTOR_SIZE = int(os.getenv("MEMORY_VECTOR_SIZE", "4096"))

# 具名向量:hybrid 检索需 dense(语义)+ sparse(BM25)双向量
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "text"

# SQLite 审计日志库路径(与 agent 其它本地数据同目录)
MEMORY_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_memory.sqlite3"

# 检索参数
MEMORY_RECALL_TOP_K = int(os.getenv("MEMORY_RECALL_TOP_K", "5"))  # recall 工具返回条数

# 写入时检索相似旧记忆的数量(供 LLM 做 ADD/UPDATE/DELETE/NONE 决策)
WRITE_SEARCH_TOP_K = int(os.getenv("MEMORY_WRITE_SEARCH_TOP_K", "10"))

# 记忆类型(chat 长期记忆),两类:
# - core: 关于用户的稳定事实(身份/职业/技术栈 + 语言/风格/格式偏好),常驻注入。
#         由原 persona + preference 合并而来(二者作用域/注入/GC 行为完全同构,
#         区分只在显示,合并抹掉这层无谓边界;抽取只需判 core-vs-episodic 真边界)。
# - episodic: 发生过什么(项目/事件/上下文),按会话隔离、按需检索注入。
MEMORY_TYPE_CORE = "core"
MEMORY_TYPE_EPISODIC = "episodic"

# 作用域:chat 三类全用 global(跨会话通用);保留常量供 payload/过滤统一使用
SCOPE_GLOBAL = "global"

# 存储层中性默认 user_id(vector 层函数默认值;chat 门面都显式传 CHAT_USER_ID)
DEFAULT_USER_ID = "local"

# chat 记忆命名空间:chat 记忆用独立 user_id(_build_filter 首个 must 即 user_id),
# 与其它命名空间天然隔离。
CHAT_USER_ID = os.getenv("CHAT_MEMORY_USER_ID", "chat:local")

# ── chat 分层记忆参数 ──
# core 层(合并后单一类型):常驻注入每轮对话,量小、都重要,不过相关性闸门。
CHAT_CORE_TYPES = [MEMORY_TYPE_CORE]                                 # 单元素,检索/剪枝/过滤统一用
CHAT_CORE_TOP_K = int(os.getenv("CHAT_CORE_TOP_K", "6"))              # 常驻 core 注入条数上限(兜底,真正限制器是字符预算)
RESIDENT_PREFERENCE_CHAR_LIMIT = int(os.getenv("MEMORY_CORE_CHARS", "800"))  # core 注入块字符上限(兼容旧名)
# P0 注入选取:core 无条数上限后,字符预算才是绑定约束(弃固定 top_k=6 的最老窗口)。
CORE_CHAR_BUDGET = int(os.getenv("MEMORY_CORE_CHAR_BUDGET", "1500")) # core 注入总字符预算,按 importance 优先填到满
# episodic 层:按需 hybrid 检索,过相关性闸门(防 Lost-in-the-Middle 噪声污染)。
RECALL_MIN_COSINE = float(_CAL.get("RECALL_MIN_COSINE",
    os.getenv("CHAT_RECALL_MIN_COSINE", "0.5")))  # 绝对余弦门(主闸门,标定优先)
RECALL_REL_RATIO = float(os.getenv("CHAT_RECALL_REL_RATIO", "0.6"))    # 相对比门(兜底,占 top1 比例)
# 写入去抖:攒 N 轮对话才抽取一次(对齐 mem0 滚动窗口,降 token 成本)。
CHAT_WRITE_EVERY_N_TURNS = int(os.getenv("CHAT_WRITE_EVERY_N_TURNS", "3"))

# 非对称 embedding(Qwen3):查询侧加英文 instruct 前缀,写入侧 doc 不加。
# 官方要求 instruct 用英文写(与中文 query 可不同语言),典型提升 1-5% 召回。
INSTRUCT_CHAT = "Retrieve facts about the user relevant to the current message"

# ── 会话隔离(路线乙)──
# episodic 记忆按会话隔离:payload.chat_id 存所属会话 id、仅本会话检索;
# core 是全局记忆,chat_id 留空(跨所有会话常驻)。
# confidence 字段兼作 importance 存储位:抽取时 LLM 打 1-10 分,归一到 0.1-1.0,
# 供检索三因子重排与 episodic GC 排序共用(一次打分两用)。

# ── episodic 检索三因子重排(对齐 Generative Agents:relevance+recency+importance)──
# 仅用于 episodic 召回排序;core 是常驻全量注入,不套三因子。
# gap-gated 稀释(调研 IR:候选相关度扎堆时 relevance 区分度差,应看相对间隔而非绝对归一):
# relevance 直接用原始 cosine(不归一);次要因子(recency+importance)乘一个动态稀释系数
# dilution = 1 - min(1, spread/GAP_REF),spread=候选 cosine 极差。
#   - 相关度拉得开(spread 大)→ dilution→0 → relevance 主导(不被次要因子干扰);
#   - 相关度扎堆(spread 小)→ dilution→1 → 放手让 recency/importance 决定(打破 tie)。
RECALL_W_RECENCY = float(os.getenv("CHAT_RECALL_W_RECENCY", "0.25"))       # 时间新近(次要,受稀释门控)
RECALL_W_IMPORTANCE = float(os.getenv("CHAT_RECALL_W_IMPORTANCE", "0.20")) # 重要性(次要,受稀释门控)
RECALL_GAP_REF = float(_CAL.get("RECALL_GAP_REF",
    os.getenv("CHAT_RECALL_GAP_REF", "0.15")))           # 多大 cosine 极差算"拉开了"(标定优先)
# recency 指数衰减半衰期(小时);用 created_at(事件新鲜度),非 last_accessed_at。
RECALL_HALFLIFE_HOURS = float(os.getenv("CHAT_RECALL_HALFLIFE_HOURS", "240"))  # 10 天

# ── episodic 会话级遗忘 GC(容量上限 + 软失效)──
# 每会话 episodic 超 cap → 写入后同步剪枝(只扫本会话,非后台全量扫描),
# 按 (verified/importance/reinforce/recency) 保留高价值,尾部 invalidate 软失效(可回溯)。
EPISODIC_CAP = int(os.getenv("CHAT_EPISODIC_CAP", "50"))                  # 每会话 episodic 上限
EPISODIC_KEEP_RATIO = float(os.getenv("CHAT_EPISODIC_KEEP_RATIO", "0.8")) # 超限剪到 cap*ratio
EPISODIC_PRUNE_GRACE_HOURS = float(os.getenv("CHAT_EPISODIC_GRACE_HOURS", "24"))  # 新记忆宽限窗,不剪

# ── 跨会话晋升(批次 B)──
# episodic 在 N 个不同 chat_id 复现(LLM 判"同一稳定事实" 而非"同一主题不同进展")→ 升 core 全局常驻。
PROMOTE_THRESHOLD = int(os.getenv("MEMORY_PROMOTE_THRESHOLD", "3"))       # 兄弟集至少覆盖 N 个 distinct chat_id
PROMOTE_SIM_COSINE = float(_CAL.get("PROMOTE_SIM_COSINE",
    os.getenv("MEMORY_PROMOTE_SIM_COSINE", "0.85"))) # 复现检测余弦门(比读路径严;标定优先)
PROMOTE_CONFIDENCE = float(os.getenv("MEMORY_PROMOTE_CONFIDENCE", "0.9"))  # 晋升 canonical 的 confidence,保 -confidence 主导排序靠前

# ── core 摘要/合并(批次 B5)──
# P0 后 core 无条数上限,只增不减(除非 UPDATE/矛盾软失效);晋升每次成功再 +1。
# 存量最终会超预算 → 靠 LLM 分组摘要压缩(对齐 MemGPT rethink)。
# 触发点:service.write_chat_memory 里 prune_global_preferences 之后异步调。
CORE_COMPACT_TRIGGER_RATIO = float(os.getenv("MEMORY_CORE_COMPACT_RATIO", "3.0"))
# 超预算多少倍触发(默认 3.0 → 4500 字符,约 150 条身份类 core)
CORE_COMPACT_MIN_GROUP = int(os.getenv("MEMORY_CORE_COMPACT_MIN_GROUP", "2"))
# 至少 N 条同主题才合并(1 条不需要"合并")

# ── 批次 E · P2:core 冲突整理(rethink)──
# 周期性/写后/一键触发 LLM 全库扫 core,判 conflicts/expired/merges → 落库。
# 与 core 摘要正交:摘要是"同主题多条压成一条",rethink 是"冲突判决 + 过期清理"。
# 三触发共用一把并发锁,防同时跑两次消耗 token + 中间状态污染。
RETHINK_CORE_INTERVAL_HOURS = float(os.getenv("MEMORY_RETHINK_INTERVAL_HOURS", "24"))
# 后台 daemon 周期(小时);拍脑袋默认,看真机 conflicts_resolved 数据再调整
RETHINK_CORE_MAX_GROUPS_PER_RUN = int(os.getenv("MEMORY_RETHINK_MAX_GROUPS", "10"))
# 单次最多处理 conflict+expired+merge 组数(防单次 LLM 输出过多误伤)
RETHINK_MIN_CORE_COUNT = int(os.getenv("MEMORY_RETHINK_MIN_CORE", "3"))
# core 少于 N 条不触发(避免噪音;至少要有比对空间才有整理意义)
RETHINK_MAX_ELAPSED_SEC = int(os.getenv("MEMORY_RETHINK_MAX_ELAPSED", "300"))
# 僵死锁兜底(秒):新请求见到超此时长的旧锁 → 视为进程僵死,强制回收
RETHINK_DAEMON_ENABLED = os.getenv("MEMORY_RETHINK_DAEMON_ENABLED", "1") == "1"
# 后台 daemon 开关(测试/调试时可关)

# ─── Chat 对话上下文压缩(Context Compaction)───────────────────
# 模型上下文窗口大小(token);标准 /v1/models 不返回此值,需手动配置。
CHAT_CONTEXT_LENGTH = int(os.getenv("CHAT_CONTEXT_LENGTH", "128000"))
# 后台预压缩阈值:超过此比例时触发后台异步压缩,当次请求不阻塞
CHAT_COMPACT_TRIGGER_RATIO = float(os.getenv("CHAT_COMPACT_TRIGGER_RATIO", "0.70"))
# 同步兜底阈值:超过此比例时当次请求同步阻塞压缩
CHAT_COMPACT_HARD_RATIO = float(os.getenv("CHAT_COMPACT_HARD_RATIO", "0.90"))
# 保留最近 N 对(user+assistant)原文不压缩
CHAT_COMPACT_KEEP_PAIRS = int(os.getenv("CHAT_COMPACT_KEEP_PAIRS", "3"))
# 摘要输出 token 上限(prompt 约束)
CHAT_COMPACT_SUMMARY_MAX_TOKENS = int(os.getenv("CHAT_COMPACT_SUMMARY_MAX_TOKENS", "800"))
# 中文字符/token 安全系数(tiktoken 对非 OpenAI 模型中文误差 20-40%,乘此系数粗估)
CHAT_TOKEN_CHAR_RATIO_CN = float(os.getenv("CHAT_TOKEN_CHAR_RATIO_CN", "1.5"))
