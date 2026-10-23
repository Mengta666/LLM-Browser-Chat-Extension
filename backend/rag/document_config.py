"""PDF 文档管线配置；不改变聊天、自动化和长期记忆设置。"""

import os
from pathlib import Path

from agent.memory.config import MEMORY_DB_PATH

STORAGE = Path(os.getenv("KB_DOCUMENT_STORAGE") or str(Path(MEMORY_DB_PATH).parent / "kb_documents")).resolve()
PARSER_URL = os.getenv("KB_PDF_PARSER_URL", "").rstrip("/")
PARSER_KEY = os.getenv("KB_PDF_PARSER_API_KEY", "")
PAGE_TIMEOUT = float(os.getenv("KB_PDF_PAGE_TIMEOUT", "190"))
TASK_TIMEOUT = float(os.getenv("KB_PDF_TASK_TIMEOUT", "7200"))
MAX_PAGES = int(os.getenv("KB_PDF_MAX_PAGES", "500"))
MAX_PIXELS = int(os.getenv("KB_PDF_MAX_PIXELS", "12000000"))
DPI = int(os.getenv("KB_PDF_RENDER_DPI", "144"))
CONCURRENCY = int(os.getenv("KB_PDF_CONCURRENCY", "1"))
CHUNK_TOKENS = int(os.getenv("KB_DOCUMENT_CHUNK_TOKENS", "512"))
TOKENIZER_URL = os.getenv("KB_EMBEDDING_TOKENIZER_URL", "").rstrip("/")
RECALL_K = int(os.getenv("KB_DOCUMENT_RECALL_K", "40"))
DENSE_K = int(os.getenv("KB_DOCUMENT_DENSE_K", str(RECALL_K)))
SPARSE_K = int(os.getenv("KB_DOCUMENT_SPARSE_K", str(RECALL_K)))
LITERAL_K = int(os.getenv("KB_DOCUMENT_LITERAL_K", str(RECALL_K)))
METADATA_K = int(os.getenv("KB_DOCUMENT_METADATA_K", "20"))
FUSION_K = int(os.getenv("KB_DOCUMENT_FUSION_K", "40"))
RERANK_K = int(os.getenv("KB_DOCUMENT_RERANK_K", "40"))
EVIDENCE_K = int(os.getenv("KB_DOCUMENT_EVIDENCE_K", "5"))
RERANK_MIN = float(os.getenv("KB_DOCUMENT_RERANK_MIN_SCORE", "0.1"))
def _positive(name, default):
    """读取正整数配置，拒绝零或负值，避免无效的资源与预算上限。"""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} 必须为正整数') from None
    if value <= 0:
        raise ValueError(f'{name} 必须为正整数')
    return value


TURN_TOKENS = _positive('KB_TURN_TOKENS', 4000)
TEXT_MAX_BLOCKS = _positive('KB_TEXT_MAX_BLOCKS', 50000)
TEXT_CONCURRENCY = _positive('KB_TEXT_CONCURRENCY', 2)
SEARCH_TOKENS = _positive('KB_SEARCH_TOKENS', 1000)
READ_TOKENS = _positive('KB_READ_TOKENS', 2000)
DIRECTORY_TOKENS = _positive('KB_DIRECTORY_TOKENS', 400)
READ_MAX_TARGETS = _positive('KB_READ_MAX_TARGETS', 4)
DECISION_MAX_ROUNDS = _positive('KB_DECISION_MAX_ROUNDS', 4)
RECOVERY_MAX_ROUNDS = _positive('KB_RECOVERY_MAX_ROUNDS', 2)
MODEL_CALL_LIMIT = _positive('KB_MODEL_CALL_LIMIT', 8)
EXECUTION_MODE = os.getenv('KB_EXECUTION_MODE', 'task')
if EXECUTION_MODE not in ('task', 'bounded'):
    raise ValueError('KB_EXECUTION_MODE 必须为 task/bounded')
NO_PROGRESS_ROUNDS = _positive('KB_NO_PROGRESS_ROUNDS', 3)
TASK_CITATION_LIMIT = _positive('KB_TASK_CITATION_LIMIT', 40)
RETRIEVAL_MODE = os.getenv('KB_RETRIEVAL_MODE', 'hybrid_structure')
if RETRIEVAL_MODE not in ('flat', 'tree_only', 'hybrid_structure'):
    raise ValueError('KB_RETRIEVAL_MODE 必须为 flat/tree_only/hybrid_structure')
ROUTE_DOC_TOP_K = _positive('KB_ROUTE_DOC_TOP_K', 3)
ROUTE_NODE_TOP_K = _positive('KB_ROUTE_NODE_TOP_K', 6)
UNIT_MAX_CHARS = _positive('KB_UNIT_MAX_CHARS', 4000)
NODE_PREVIEW_CHARS = _positive('KB_NODE_PREVIEW_CHARS', 120)
PROOF_PASSAGE_CHARS = _positive('KB_PROOF_PASSAGE_CHARS', 600)
READ_PASSAGE_CHARS = _positive('KB_READ_PASSAGE_CHARS', 1200)
WORKSPACE_ENABLED = os.getenv('KB_WORKSPACE_ENABLED', 'true').lower() in ('true', '1')
WORKSPACE_TRIGGER = float(os.getenv('KB_WORKSPACE_TRIGGER_RATIO', '.8'))
WORKSPACE_TARGET = float(os.getenv('KB_WORKSPACE_TARGET_RATIO', '.6'))
if not 0 < WORKSPACE_TARGET < WORKSPACE_TRIGGER < 1:
    raise ValueError('KB_WORKSPACE ratios require 0 < target < trigger < 1')
WORKSPACE_COMPACT = os.getenv('KB_WORKSPACE_COMPACT_ENABLED', 'true').lower() in ('true','1')
WORKSPACE_BATCH_TOKENS = _positive('KB_WORKSPACE_BATCH_TOKENS', 6000)
WORKSPACE_OUTPUT_TOKENS = _positive('KB_WORKSPACE_OUTPUT_TOKENS', 2048)
WORKSPACE_TIMEOUT = _positive('KB_WORKSPACE_COMPACT_TIMEOUT', 60)
WORKSPACE_MIN_TOKENS = _positive('KB_WORKSPACE_MIN_TOKENS', 512)


def budget_settings():
    """取得本次知识库请求需要固定的预算及工作区配置快照。"""
    return {key: globals()[key] for key in ('TURN_TOKENS', 'SEARCH_TOKENS', 'READ_TOKENS',
            'DIRECTORY_TOKENS', 'READ_MAX_TARGETS', 'DECISION_MAX_ROUNDS', 'RECOVERY_MAX_ROUNDS', 'MODEL_CALL_LIMIT',
            'EXECUTION_MODE', 'NO_PROGRESS_ROUNDS', 'WORKSPACE_ENABLED', 'WORKSPACE_TRIGGER', 'WORKSPACE_TARGET',
            'WORKSPACE_COMPACT', 'WORKSPACE_BATCH_TOKENS', 'WORKSPACE_OUTPUT_TOKENS', 'WORKSPACE_TIMEOUT', 'WORKSPACE_MIN_TOKENS')}


def document_path(doc_id: str, *parts: str) -> Path:
    """构造文档存储路径并验证其仍位于指定文档目录内，防止路径越界。"""
    if not doc_id.startswith("doc_") or not doc_id[4:].isalnum():
        raise ValueError("invalid document id")
    path = STORAGE.joinpath(doc_id, *parts).resolve()
    if not path.is_relative_to(STORAGE / doc_id):
        raise ValueError("invalid document path")
    return path
