import os
import logging
from openai import AsyncOpenAI


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ----------------------------
# Config & Logging Setup
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("memory-mcp")

DATABASE_URL = os.environ["DATABASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gpt-5-mini")
CONFLICT_MODEL = os.getenv("CONFLICT_MODEL", "gpt-5-nano")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))

DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10"))
DEFAULT_LIST_LIMIT = int(os.getenv("DEFAULT_LIST_LIMIT", "50"))

OPENAI_TIMEOUT_S = float(os.getenv("OPENAI_TIMEOUT_S", "60"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "5"))
MAX_CONCURRENT_API_CALLS = int(os.getenv("MAX_CONCURRENT_API_CALLS", "5"))

PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "10"))

EXTRACT_REASONING = os.getenv("EXTRACT_REASONING", "low")
CONFLICT_REASONING = os.getenv("CONFLICT_REASONING", "minimal")
DUP_THRESHOLD = float(os.getenv("DUP_THRESHOLD", "0.95"))
CONFLICT_THRESHOLD = float(os.getenv("CONFLICT_THRESHOLD", "0.55"))
RELATES_TO_THRESHOLD = float(os.getenv("RELATES_TO_THRESHOLD", "0.65"))
MIN_SECTION_LENGTH = int(os.getenv("MIN_SECTION_LENGTH", "100"))
MAX_TAXONOMY_PATHS = int(os.getenv("MAX_TAXONOMY_PATHS", "40"))
PRODUCTION_PORT = int(os.getenv("PRODUCTION_PORT", "8766"))
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8767"))
STAGING_RETENTION_DAYS = int(os.getenv("STAGING_RETENTION_DAYS", "7"))

API_KEY = os.getenv("API_KEY")  # Optional; enables Bearer token auth when set
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "api-key")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", API_KEY)

MAX_MEMORIZE_TEXT_LENGTH = int(os.getenv("MAX_MEMORIZE_TEXT_LENGTH", "500000"))

# Context Store
CONTEXT_DEFAULT_TTL_HOURS = int(os.getenv("CONTEXT_DEFAULT_TTL_HOURS", "24"))
CONTEXT_MAX_VALUE_LENGTH = int(os.getenv("CONTEXT_MAX_VALUE_LENGTH", "50000"))
CONTEXT_MAX_KEY_LENGTH = int(os.getenv("CONTEXT_MAX_KEY_LENGTH", "200"))

# Feedback-guided retrieval (safe defaults)
FEEDBACK_RERANK_ENABLED = _env_bool("FEEDBACK_RERANK_ENABLED", False)
FEEDBACK_MAX_DELTA = max(0.0, float(os.getenv("FEEDBACK_MAX_DELTA", "0.05")))
FEEDBACK_HALF_LIFE_DAYS = max(1.0, float(os.getenv("FEEDBACK_HALF_LIFE_DAYS", "30")))
CANONICAL_MIN_IN_TOPK = max(0, int(os.getenv("CANONICAL_MIN_IN_TOPK", "2")))
HISTORICAL_MIN_IN_TOPK = max(0, int(os.getenv("HISTORICAL_MIN_IN_TOPK", "1")))
FEEDBACK_EXPLORATION_SLOTS = max(0, int(os.getenv("FEEDBACK_EXPLORATION_SLOTS", "0")))
HISTORICAL_BASE_SCORE_MULTIPLIER = max(0.0, min(1.0, float(os.getenv("HISTORICAL_BASE_SCORE_MULTIPLIER", "0.85"))))
TIER_LLM_INFERENCE_ENABLED = _env_bool("TIER_LLM_INFERENCE_ENABLED", True)

logger.info(
    "Config loaded: EMBEDDING_MODEL=%s EXTRACT_MODEL=%s CONFLICT_MODEL=%s EMBED_DIM=%d "
    "SEARCH_LIMIT=%d LIST_LIMIT=%d TIMEOUT=%.1fs MAX_RETRIES=%d CONCURRENCY=%d PG_POOL=%d-%d "
    "FEEDBACK_RERANK=%s FEEDBACK_MAX_DELTA=%.3f FEEDBACK_HALF_LIFE_DAYS=%.1f "
    "HIST_BASE_MULT=%.3f TIER_LLM_INFERENCE=%s",
    EMBEDDING_MODEL, EXTRACT_MODEL, CONFLICT_MODEL, EMBED_DIM,
    DEFAULT_SEARCH_LIMIT, DEFAULT_LIST_LIMIT,
    OPENAI_TIMEOUT_S, OPENAI_MAX_RETRIES, MAX_CONCURRENT_API_CALLS,
    PG_POOL_MIN, PG_POOL_MAX,
    FEEDBACK_RERANK_ENABLED, FEEDBACK_MAX_DELTA, FEEDBACK_HALF_LIFE_DAYS,
    HISTORICAL_BASE_SCORE_MULTIPLIER, TIER_LLM_INFERENCE_ENABLED,
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_S)
