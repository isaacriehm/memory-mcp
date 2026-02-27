import os
import logging
from openai import AsyncOpenAI

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

MAX_MEMORIZE_TEXT_LENGTH = int(os.getenv("MAX_MEMORIZE_TEXT_LENGTH", "500000"))

# Context Store
CONTEXT_DEFAULT_TTL_HOURS = int(os.getenv("CONTEXT_DEFAULT_TTL_HOURS", "24"))
CONTEXT_MAX_VALUE_LENGTH = int(os.getenv("CONTEXT_MAX_VALUE_LENGTH", "50000"))
CONTEXT_MAX_KEY_LENGTH = int(os.getenv("CONTEXT_MAX_KEY_LENGTH", "200"))

logger.info(
    "Config loaded: EMBEDDING_MODEL=%s EXTRACT_MODEL=%s CONFLICT_MODEL=%s EMBED_DIM=%d "
    "SEARCH_LIMIT=%d LIST_LIMIT=%d TIMEOUT=%.1fs MAX_RETRIES=%d CONCURRENCY=%d PG_POOL=%d-%d",
    EMBEDDING_MODEL, EXTRACT_MODEL, CONFLICT_MODEL, EMBED_DIM,
    DEFAULT_SEARCH_LIMIT, DEFAULT_LIST_LIMIT,
    OPENAI_TIMEOUT_S, OPENAI_MAX_RETRIES, MAX_CONCURRENT_API_CALLS,
    PG_POOL_MIN, PG_POOL_MAX,
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_S)