import re
import uuid
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, TypeVar, Callable, Awaitable, Any
from uuid import UUID

T = TypeVar("T")

from config import logger, OPENAI_MAX_RETRIES


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _add_ttl_warning(item: dict[str, Any], updated_at: datetime) -> None:
    metadata = item.get("metadata", {})
    ttl_days = metadata.get("ttl_days")
    if ttl_days is not None:
        if _now() > updated_at + timedelta(days=ttl_days):
            item["is_expired"] = True
            item["warning"] = f"TTL EXPIRED: This memory (ID: {item['id']}) may be outdated. Please verify with the user and update if necessary."

def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vec) + "]"

def _is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status not in (400, 401, 403)
    return True

async def _with_retries(fn: Callable[[], Awaitable[T]], *, label: str = "openai_call", max_retries: int = OPENAI_MAX_RETRIES) -> T:
    logger.debug("Starting %s with up to %d retries", label, max_retries)
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries):
        t0 = time.perf_counter()
        try:
            result = await fn()
            elapsed = time.perf_counter() - t0
            logger.debug("Successfully executed %s on attempt %d in %.2fs", label, attempt + 1, elapsed)
            return result
        except Exception as e:
            elapsed = time.perf_counter() - t0
            last_exc = e
            logger.warning("%s failed on attempt %d in %.2fs: %s", label, attempt + 1, elapsed, e)
            if not _is_retryable(e):
                logger.error("Exception in %s is not retryable. Aborting retries.", label)
                raise
            sleep_s = min(2.0 ** attempt, 10.0) + (0.05 * attempt)
            logger.info("Sleeping for %.2fs before retrying %s...", sleep_s, label)
            await asyncio.sleep(sleep_s)

    logger.error("Exhausted all %d retries for %s. Last exception: %s", max_retries, label, last_exc)
    raise last_exc if last_exc else RuntimeError(f"{label} retry failed")

def generate_deterministic_id(text: str) -> UUID:
    """Idempotency: Generate a deterministic UUID from normalized input text without data truncation."""
    normalized = " ".join(text.strip().split()).lower()
    generated_id = uuid.uuid5(uuid.NAMESPACE_OID, normalized)
    logger.debug("Generated deterministic UUID %s for text of length %d", generated_id, len(text))
    return generated_id

def sanitize_ltree_label(text: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', str(text)).strip('_').lower()
    return cleaned if cleaned else "unknown"

def sanitize_ltree_path(path: str) -> str:
    path = path.replace('/', '.').replace('\\', '.')
    segments = path.split('.')
    sanitized = [sanitize_ltree_label(s) for s in segments if s.strip()]
    if sanitized and sanitized[0] == "user":
        logger.warning("sanitize_ltree_path: rewrote 'user' root to 'profile' in path '%s'", path)
        sanitized[0] = "profile"
    sanitized = sanitized[:6]
    return '.'.join(sanitized) if sanitized else 'reference.unknown'

def truncate_text(text: str, max_length: int = 12000) -> str:
    if len(text) <= max_length:
        return text
    half = max_length // 2
    return text[:half] + "\n...[TRUNCATED]...\n" + text[-half:]