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
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} 必须为正整数') from None
    if value <= 0:
        raise ValueError(f'{name} 必须为正整数')
    return value


TURN_TOKENS = _positive('KB_TURN_TOKENS', 4000)
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


def budget_settings():
    return {key: globals()[key] for key in ('TURN_TOKENS', 'SEARCH_TOKENS', 'READ_TOKENS',
            'DIRECTORY_TOKENS', 'READ_MAX_TARGETS', 'DECISION_MAX_ROUNDS', 'RECOVERY_MAX_ROUNDS', 'MODEL_CALL_LIMIT',
            'EXECUTION_MODE', 'NO_PROGRESS_ROUNDS')}


def document_path(doc_id: str, *parts: str) -> Path:
    if not doc_id.startswith("doc_") or not doc_id[4:].isalnum():
        raise ValueError("invalid document id")
    path = STORAGE.joinpath(doc_id, *parts).resolve()
    if not path.is_relative_to(STORAGE / doc_id):
        raise ValueError("invalid document path")
    return path
