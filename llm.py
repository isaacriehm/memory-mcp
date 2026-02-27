import json
import traceback
from typing import Any, TypedDict

from config import (
    logger, openai_client, EMBEDDING_MODEL, EMBED_DIM, EXTRACT_MODEL, CONFLICT_MODEL,
    MIN_SECTION_LENGTH
)
from utils import _with_retries, sanitize_ltree_path, truncate_text


def _parse_json_safe(raw: str) -> dict:
    """Strip markdown backticks and whitespace before parsing. Prevents reference.unknown fallback when LLM returns ```json {...}```."""
    if not raw:
        return {}
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned or "{}")


class ConflictResolutionResult(TypedDict):
    resolution: str
    updated_text: str


async def embed(text: str) -> list[float]:
    logger.debug("Requesting embedding for text of length %d", len(text))

    async def _call():
        resp = await openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
        return resp.data[0].embedding

    vec = await _with_retries(_call, label=f"embed({EMBEDDING_MODEL})")
    if len(vec) != EMBED_DIM:
        logger.error("Embedding dim mismatch: got %d expected %d", len(vec), EMBED_DIM)
        raise ValueError(f"Embedding dim mismatch: got {len(vec)} expected {EMBED_DIM}")
    logger.debug("Successfully generated embedding of dimension %d", len(vec))
    return vec


SEMANTIC_SECTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category_path": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "volatility_class": {
                        "type": "string",
                        "enum": ["static", "high", "medium", "low"],
                    },
                },
                "required": ["category_path", "content", "tags", "volatility_class"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sections"],
    "additionalProperties": False,
}


async def extract_semantic_sections(text: str, active_taxonomy: str = "") -> list[dict[str, Any]]:
    """
    LLM-driven semantic extraction. Ingests the complete payload in a single unbounded call.
    Divides input into cohesive logical units with taxonomy, tags, and volatility.
    """
    logger.debug("Extracting semantic sections from text of length %d", len(text))

    system_content = (
        "Analyze the input data. Divide it into strictly cohesive logical units. "
        "Output the exact text for each unit into the 'content' field. "
        "Assign a broad taxonomy path (2-4 levels) to each unit.\n\n"
        "STRICT COHESION RULE: A unit is cohesive ONLY if it covers ONE specific sub-topic. "
        "Psychology/ADHD and Fitness/Gym must ALWAYS be separate sections. "
        "Never mix distinct domains (e.g., health + tech, lifestyle + projects) in a single section.\n\n"
        "STRICT TAXONOMY RULES:\n"
        "1. PATH SELECTION: Check the EXISTING PATHS list below. Reuse an existing path ONLY if "
        "the content is a direct topical match. If no existing path fits, create a new one under "
        "the correct L1 root. Do NOT force-fit content into an existing path just because it is "
        "the closest available option. A wrong existing path is always worse than a correct new path."
        "2. L1 ROOT DOMAINS (use ONLY these five):\n"
        "   - 'profile': Personal identity, demographics, health, psychology, and personal habits.\n"
        "   - 'projects': Specific work initiatives, software products (e.g., MyApp), and tasks.\n"
        "   - 'organizations': Business entities, companies, and professional structures.\n"
        "   - 'concepts': Abstract ideas, technology stacks, and general knowledge.\n"
        "   - 'reference': System data, primers, and documentation.\n"
        "   CRUCIAL: NEVER use 'user' as an L1 root. Use 'profile' instead.\n\n"
        "3. MAPPING LOGIC:\n"
        "   - Professional content (Sales, ICP, S3, Auth) MUST go under 'projects.<name>' or 'organizations'.\n"
        "   - Personal content (Nutrition, Supplements, Fitness) MUST go under 'profile.lifestyle' or 'profile.health'.\n"
        "   - NEVER mix professional tech/sales content into 'profile.health' or 'profile.lifestyle'.\n\n"
        "4. NOTATION: Strict dot-notation. Preferred depth: 2-4 levels. Avoid hyper-specific file paths or endpoint names. "
        "Never use 'personal' as an L2 under 'profile' (e.g. use profile.identity, not profile.personal.identity).\n\n"
        "CHUNKING RULES: Each section MUST be at least 3 sentences or 150 words. Do NOT split a single coherent topic into micro-chunks. Prefer fewer, larger sections over many small ones. A single document should rarely exceed 5 sections.\n\n"
        f"EXISTING PATHS FOR REFERENCE:\n{active_taxonomy}"
    )

    async def _call():
        completion = await openai_client.chat.completions.create(
            model=EXTRACT_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "semantic_sections",
                    "schema": SEMANTIC_SECTIONS_SCHEMA,
                    "strict": True,
                },
            },
            reasoning_effort="low",
        )
        return completion.choices[0].message.content

    try:
        raw = await _with_retries(_call, label=f"extract_semantic_sections({EXTRACT_MODEL})")
        parsed = _parse_json_safe(raw or '{"sections":[]}')
        sections = parsed.get("sections", [])
        if not sections:
            logger.warning("extract_semantic_sections returned no sections; using full text as single section")
            return [
                {
                    "category_path": "reference.unknown",
                    "content": text,
                    "tags": [],
                    "volatility_class": "low",
                }
            ]
        for s in sections:
            s["category_path"] = sanitize_ltree_path(
                s.get("category_path", "reference.unknown") or "reference.unknown"
            )
            s["volatility_class"] = s.get("volatility_class", "low")
            if s["volatility_class"] not in ("static", "high", "medium", "low"):
                s["volatility_class"] = "low"
        
        sections = [s for s in sections if len(s.get("content", "").strip()) >= MIN_SECTION_LENGTH]
        logger.debug("Extracted %d semantic sections after length filtering", len(sections))
        return sections
    except Exception as e:
        logger.error("Semantic section extraction failed: %s\n%s", e, traceback.format_exc())
        return [
            {
                "category_path": "reference.unknown",
                "content": text,
                "tags": [],
                "volatility_class": "low",
            }
        ]


async def evaluate_conflict(old_text: str, new_text: str) -> ConflictResolutionResult:
    """
    Strict fact-isolation arbiter. Isolates factual mutations; returns supersedes with
    isolated new state only when a fact is contradicted or updated.
    """
    logger.debug("Evaluating conflict between chunks of length %d and %d", len(old_text), len(new_text))
    safe_old = truncate_text(old_text, 6000)
    safe_new = truncate_text(new_text, 6000)

    async def _call():
        completion = await openai_client.chat.completions.create(
            model=CONFLICT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict factual arbiter enforcing absolute knowledge singularity.\n\n"
                        "PROCEDURE:\n"
                        "STEP 1 — Extract every atomic factual claim from OLD TEXT.\n"
                        "STEP 2 — Extract every atomic factual claim from NEW TEXT.\n"
                        "STEP 3 — Identify any claim in OLD TEXT that is DIRECTLY CONTRADICTED "
                        "or MUTATED by NEW TEXT (e.g. a price changed, a name changed, a date "
                        "changed, a status changed, a quantity changed, a value was corrected).\n\n"
                        "DECISION RULES — apply strictly, no exceptions:\n"
                        "• If ANY factual mutation is detected → resolution MUST be \"supersedes\". "
                        "When supersedes: updated_text MUST be the full original paragraph with the "
                        "new/corrected fact integrated into it, preserving surrounding context. "
                        "Do NOT output only the isolated changed fact.\n"
                        "• If NEW TEXT ONLY adds information without contradicting a single claim "
                        "in OLD TEXT → resolution is \"merges\". Set updated_text to a unified "
                        "text that integrates both without duplication.\n\n"
                        "CRITICAL: \"merges\" is ONLY valid when every single claim in OLD TEXT "
                        "remains fully true and uncontradicted in the context of NEW TEXT. "
                        "A single mutated fact — however minor — forces \"supersedes\". "
                        "When supersedes, updated_text must be the full original paragraph with the fact integrated, not the isolated fragment.\n\n"
                        "Output JSON with keys 'resolution' and 'updated_text'."
                    ),
                },
                {"role": "user", "content": f"<old_text>{safe_old}</old_text>\n\n<new_text>{safe_new}</new_text>"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "conflict",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "resolution": {"type": "string", "enum": ["supersedes", "merges"]},
                            "updated_text": {"type": "string"},
                        },
                        "required": ["resolution", "updated_text"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            reasoning_effort="minimal",
            max_completion_tokens=8000,
        )
        return completion.choices[0].message.content

    try:
        raw = await _with_retries(_call, label=f"evaluate_conflict({CONFLICT_MODEL})")
        parsed = _parse_json_safe(raw or "{}")
        resolution = parsed.get("resolution", "supersedes")
        updated_text = parsed.get("updated_text", new_text)
        logger.debug("Conflict resolved as '%s' with text length %d", resolution, len(updated_text))
        return {"resolution": resolution, "updated_text": updated_text}

    except Exception as e:
        logger.error("Conflict evaluation failed: %s\n%s", e, traceback.format_exc())
        return {"resolution": "supersedes", "updated_text": new_text}

async def summarize_user_profile(chunks: list[str]) -> str:
    """
    Takes all profile.* memory chunks and produces a compact natural-language
    summary of the user for inclusion in the system primer.
    """
    if not chunks:
        return ""

    combined = "\n\n---\n\n".join(chunks)

    system_content = (
        "You are writing the User Context section of a system primer for an AI agent. "
        "The agent will read this at the start of every session to understand who it is working with.\n\n"
        "You will be given a set of memory records about the user. Write a concise, natural-language "
        "summary of 3-6 sentences. Write it as a briefing — who this person is, what they are currently "
        "doing, what matters to them. Do not list facts as bullet points. Do not use headers. "
        "Do not reproduce the raw memory content. Write prose, as if briefing a colleague before a meeting.\n\n"
        "Include: identity basics (name, age, location, occupation), active pursuits and current projects, "
        "health or lifestyle protocols if ongoing, personality or relational traits that would affect how "
        "an agent should interact with them.\n\n"
        "Omit: resolved past events, granular historical detail, anything that does not affect how an agent "
        "should approach a session today."
    )

    try:
        async def _call():
            return await openai_client.chat.completions.create(
                model=EXTRACT_MODEL,
                reasoning_effort="low",
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": f"User memory records:\n\n{combined}"}
                ],
                max_completion_tokens=10000,
            )

        response = await _with_retries(_call, label=f"summarize_user_profile({EXTRACT_MODEL})")
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("summarize_user_profile failed: %s\n%s", e, traceback.format_exc())
        return ""
