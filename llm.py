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


class ConflictResolutionResult(TypedDict, total=False):
    resolution: str
    updated_text: str
    reason_summary: str
    changed_claims: list[str]
    confidence_score: float
    evidence_used: str


class SemanticDiffResult(TypedDict):
    overview: str
    added_points: list[str]
    removed_points: list[str]
    changed_points: list[str]
    risk_notes: list[str]


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
                    "category_path": {
                        "type": "string",
                        "pattern": "^(profile|projects|organizations|concepts|reference)(\\.[a-z][a-z0-9_]{0,}){1,4}$",
                    },
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "volatility_class": {
                        "type": "string",
                        "enum": ["static", "high", "medium", "low"],
                    },
                    "suggested_tier": {
                        "type": "string",
                        "enum": ["canonical", "historical", "ephemeral"],
                    },
                },
                "required": ["category_path", "content", "tags", "volatility_class", "suggested_tier"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sections"],
    "additionalProperties": False,
}


SEMANTIC_DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "added_points": {"type": "array", "items": {"type": "string"}},
        "removed_points": {"type": "array", "items": {"type": "string"}},
        "changed_points": {"type": "array", "items": {"type": "string"}},
        "risk_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overview",
        "added_points",
        "removed_points",
        "changed_points",
        "risk_notes",
    ],
    "additionalProperties": False,
}


def _normalize_bullets(values: Any, max_bullets: int) -> list[str]:
    if not isinstance(values, list):
        return []
    bullets: list[str] = []
    for item in values:
        text = str(item).replace("\n", " ").strip()
        if text:
            bullets.append(text[:280])
        if len(bullets) >= max_bullets:
            break
    return bullets


def _looks_like_decision_record(content: str) -> bool:
    text = (content or "").lower()
    has_decision = "decision" in text
    has_rationale = "rationale" in text
    has_alternatives = "alternatives considered" in text or "alternatives" in text
    has_rejected = "rejected because" in text or "rejected" in text
    return has_decision and has_rationale and (has_alternatives or has_rejected)


def _force_decisions_category(path: str) -> str:
    safe = sanitize_ltree_path(path or "projects.general.decisions")
    parts = [p for p in safe.split(".") if p]
    if len(parts) >= 2 and parts[0] == "projects":
        project = parts[1]
    else:
        project = "general"
    return f"projects.{project}.decisions"


def _normalize_project_root(path: str) -> str | None:
    safe = sanitize_ltree_path(path or "")
    parts = [p for p in safe.split(".") if p]
    if len(parts) >= 2 and parts[0] == "projects":
        return f"projects.{parts[1]}"
    return None


def _default_identifiers_for_root(root: str) -> list[str]:
    root_norm = _normalize_project_root(root)
    if not root_norm:
        return []
    slug = root_norm.split(".", 1)[1]
    parts = [p for p in slug.replace("-", "_").split("_") if len(p) >= 4]
    candidates: list[str] = [slug, slug.replace("_", " "), slug.replace("_", "-")]
    candidates.extend(parts)
    seen: set[str] = set()
    deduped: list[str] = []
    for token in candidates:
        normalized = token.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(token.strip())
    return deduped


def _identifiers_for_root(root: str) -> list[str]:
    return _default_identifiers_for_root(root)


def _has_strong_new_root_signal(content_lower: str, root: str) -> bool:
    root_norm = _normalize_project_root(root)
    if not root_norm:
        return False

    slug = root_norm.split(".", 1)[1]
    slug_variants = [slug, slug.replace("_", " "), slug.replace("_", "-")]
    for token in slug_variants:
        token_norm = token.strip().lower()
        # Strong exact slug signal (single or multi-word) is enough to admit.
        if len(token_norm) >= 4 and token_norm in content_lower:
            return True

    parts = [p for p in slug.replace("-", "_").split("_") if len(p) >= 4]
    part_matches = sum(1 for p in set(parts) if p.lower() in content_lower)
    # If slug isn't mentioned directly, require at least two meaningful part matches.
    return part_matches >= 2


def _resolve_known_project_roots(known_project_roots: list[str] | None) -> list[str]:
    resolved: set[str] = set()
    for root in known_project_roots or []:
        normalized = _normalize_project_root(root)
        if normalized:
            resolved.add(normalized)
    return sorted(resolved)


def _build_project_namespace_block(known_project_roots: list[str] | None) -> str:
    roots = _resolve_known_project_roots(known_project_roots)
    if not roots:
        return ""

    lines = [
        "KNOWN PROJECT NAMESPACES (prefer these roots for project content):"
    ]
    for root in roots:
        identifiers = _identifiers_for_root(root)
        identifier_preview = ", ".join(identifiers[:5])
        if identifier_preview:
            lines.append(f"- {root} (root-derived identifiers: {identifier_preview})")
        else:
            lines.append(f"- {root}")
    lines.append(
        "Choose the correct project using subject matter and identifiers, not frequency bias from other projects. "
        "If no known root is a good fit and the content clearly introduces a new project, create a new root "
        "using `projects.<slug>`."
    )
    return "\n".join(lines)


def _identifier_score(text_lower: str, identifiers: list[str]) -> int:
    score = 0
    for token in identifiers:
        normalized = str(token).strip().lower()
        if normalized and normalized in text_lower:
            score += 1
    return score


def _best_project_root_for_content(
    content: str,
    candidate_roots: list[str],
) -> tuple[str | None, int]:
    text_lower = (content or "").lower()
    best_root: str | None = None
    best_score = 0
    for root in candidate_roots:
        identifiers = _identifiers_for_root(root)
        score = _identifier_score(text_lower, identifiers)
        if score > best_score:
            best_root = root
            best_score = score
    return best_root, best_score


def _rewrite_project_root(category_path: str, new_root: str) -> str:
    safe = sanitize_ltree_path(category_path or "reference.unknown")
    suffix = [part for part in safe.split(".") if part][2:]
    rewritten = new_root
    if suffix:
        rewritten = f"{new_root}." + ".".join(suffix)
    return sanitize_ltree_path(rewritten)


_VALID_L1_ROOTS = frozenset(["profile", "projects", "organizations", "concepts", "reference"])


def _validate_section_paths(sections: list[dict[str, Any]]) -> None:
    """
    Lightweight sanity pass: enforces L1 root domain and path depth on all extracted sections
    before they reach the dedup/conflict engine. Prevents taxonomy leaks like misclassified
    cross-project paths or invalid root domains (e.g. 'user' instead of 'profile').
    """
    for section in sections:
        path = section.get("category_path", "reference.unknown")
        parts = [p for p in path.split(".") if p]
        if not parts:
            section["category_path"] = "reference.unknown"
            logger.warning("Empty category_path replaced with reference.unknown")
            continue
        l1 = parts[0]
        if l1 not in _VALID_L1_ROOTS:
            if l1 == "user":
                # Common LLM mistake: 'user.X' should be 'profile.X'
                tail = ".".join(parts[1:]) if len(parts) > 1 else "identity"
                section["category_path"] = sanitize_ltree_path(f"profile.{tail}")
                logger.warning("Normalized invalid L1 root 'user' -> 'profile' for path '%s'", path)
            else:
                section["category_path"] = "reference.unknown"
                logger.warning(
                    "Invalid L1 root '%s' in path '%s'; replaced with reference.unknown", l1, path
                )
            continue
        # Enforce depth: must be 2–5 levels (l1.l2[.l3.l4.l5])
        if len(parts) < 2:
            section["category_path"] = sanitize_ltree_path(f"{l1}.general")
            logger.warning("Path '%s' is too shallow; expanded to '%s.general'", path, l1)
        elif len(parts) > 5:
            section["category_path"] = sanitize_ltree_path(".".join(parts[:5]))
            logger.warning("Path '%s' exceeds max depth 5; truncated", path)


def _validate_project_classification(
    sections: list[dict[str, Any]],
    known_project_roots: list[str] | None,
) -> None:
    allowed_roots = _resolve_known_project_roots(known_project_roots)
    if not allowed_roots:
        return

    allowed_set = set(allowed_roots)

    for section in sections:
        original_path = section.get("category_path", "reference.unknown")
        current_root = _normalize_project_root(original_path)
        if not current_root:
            continue

        detected_root, detected_score = _best_project_root_for_content(
            section.get("content", ""),
            allowed_roots,
        )
        content_lower = str(section.get("content", "")).lower()
        assigned_score = _identifier_score(
            content_lower,
            _identifiers_for_root(current_root),
        )

        if current_root not in allowed_set:
            new_root_signal = _has_strong_new_root_signal(content_lower, current_root)
            if new_root_signal and assigned_score >= max(1, detected_score):
                section["category_path"] = sanitize_ltree_path(original_path)
                logger.warning(
                    "Admitted new project root '%s' for path '%s' based on strong slug evidence",
                    current_root,
                    original_path,
                )
                continue

            if detected_root and detected_score > 0:
                section["category_path"] = _rewrite_project_root(original_path, detected_root)
                logger.warning(
                    "Reclassified unknown project root '%s' -> '%s' for path '%s'",
                    current_root,
                    detected_root,
                    original_path,
                )
            else:
                section["category_path"] = "projects.general"
                logger.warning(
                    "Project root '%s' is not in known namespaces and no identifier match was found; "
                    "falling back to projects.general",
                    current_root,
                )
            continue

        # Known root but content has ZERO identifier match — likely LLM frequency bias.
        if assigned_score == 0:
            if detected_root and detected_score > 0:
                section["category_path"] = _rewrite_project_root(original_path, detected_root)
                logger.warning(
                    "Reclassified zero-match known root '%s' -> '%s' for path '%s'",
                    current_root,
                    detected_root,
                    original_path,
                )
            else:
                section["category_path"] = "projects.general"
                logger.warning(
                    "Known root '%s' has zero identifier match and no better alternative; "
                    "falling back to projects.general for path '%s'",
                    current_root,
                    original_path,
                )
            continue

        if detected_root and detected_root != current_root and detected_score > assigned_score:
            section["category_path"] = _rewrite_project_root(original_path, detected_root)
            logger.warning(
                "Adjusted project root '%s' -> '%s' for path '%s' based on identifier match",
                current_root,
                detected_root,
                original_path,
            )


async def extract_semantic_sections(
    text: str,
    active_taxonomy: str = "",
    known_project_roots: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    LLM-driven semantic extraction. Ingests the complete payload in a single unbounded call.
    Divides input into cohesive logical units with taxonomy, tags, and volatility.
    """
    logger.debug("Extracting semantic sections from text of length %d", len(text))

    project_namespace_block = _build_project_namespace_block(known_project_roots)
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
        "TIER CLASSIFICATION RULES:\n"
        "1. `canonical`: strategic decisions, architecture commitments, durable product direction, business context, principles.\n"
        "2. `historical`: implementation notes, migrations, refactor context, superseded operational details.\n"
        "3. `ephemeral`: temporary session/task state. This should NOT be ingested as memory.\n"
        "4. For ingestion payloads, do NOT assign `ephemeral`; use canonical or historical.\n"
        "5. If content uses structured design-decision format (e.g., DECISION/RATIONALE/ALTERNATIVES/REJECTED), classify as `canonical` "
        "and choose a category ending in `.decisions` (typically `projects.<project>.decisions`). Never classify these as historical.\n\n"
        "CHUNKING RULES: Each section MUST be at least 3 sentences or 150 words. Do NOT split a single coherent topic into micro-chunks. Prefer fewer, larger sections over many small ones. A single document should rarely exceed 5 sections.\n\n"
        f"{project_namespace_block}\n\n"
        f"EXISTING PATHS FOR REFERENCE:\n{active_taxonomy}\n\n"
        "COMPLETENESS VERIFICATION: Before returning, scan the full document from start to finish and confirm every distinct semantic region is represented by a section. "
        "Do not merge unrelated topics into a single section. "
        "If you notice a distinct topic that has no corresponding section, add it. "
        "Your section list must collectively cover the entire document — no region should be silently dropped."
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
                    "suggested_tier": "canonical",
                }
            ]
        for s in sections:
            s["category_path"] = sanitize_ltree_path(
                s.get("category_path", "reference.unknown") or "reference.unknown"
            )
            s["volatility_class"] = s.get("volatility_class", "low")
            if s["volatility_class"] not in ("static", "high", "medium", "low"):
                s["volatility_class"] = "low"
            suggested_tier = str(s.get("suggested_tier", "canonical")).strip().lower()
            if suggested_tier not in ("canonical", "historical", "ephemeral"):
                suggested_tier = "canonical"
            s["suggested_tier"] = suggested_tier
            if _looks_like_decision_record(s.get("content", "")):
                s["category_path"] = _force_decisions_category(s["category_path"])
                s["suggested_tier"] = "canonical"

        _validate_section_paths(sections)
        _validate_project_classification(sections, known_project_roots)
        
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
                "suggested_tier": "canonical",
            }
        ]


async def semantic_diff(left_text: str, right_text: str, max_bullets: int = 12) -> SemanticDiffResult:
    """Compare two memory states and return semantic meaning changes."""
    try:
        parsed_max_bullets = int(max_bullets)
    except (TypeError, ValueError):
        parsed_max_bullets = 12
    bullet_limit = max(1, min(parsed_max_bullets, 20))
    safe_left = truncate_text(left_text, 9000)
    safe_right = truncate_text(right_text, 9000)

    async def _call():
        completion = await openai_client.chat.completions.create(
            model=EXTRACT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Compare LEFT and RIGHT as semantic states. Focus on meaning changes, not phrasing.\n"
                        f"Return concise arrays with at most {bullet_limit} bullet strings per array.\n"
                        "Definitions:\n"
                        "- added_points: claims present in RIGHT but not in LEFT.\n"
                        "- removed_points: claims present in LEFT but absent in RIGHT.\n"
                        "- changed_points: claims that exist in both but materially changed values/meaning.\n"
                        "- risk_notes: potential ambiguity, contradiction, or migration risk introduced by the change.\n"
                        "Keep each bullet short and actionable. No markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": f"<left>\n{safe_left}\n</left>\n\n<right>\n{safe_right}\n</right>",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "semantic_diff",
                    "schema": SEMANTIC_DIFF_SCHEMA,
                    "strict": True,
                },
            },
            reasoning_effort="low",
            max_completion_tokens=3000,
        )
        return completion.choices[0].message.content

    try:
        raw = await _with_retries(_call, label=f"semantic_diff({EXTRACT_MODEL})")
        parsed = _parse_json_safe(raw or "{}")
        overview = str(parsed.get("overview", "")).strip()
        if not overview:
            overview = "Semantic changes detected between compared memory states."
        return {
            "overview": overview[:500],
            "added_points": _normalize_bullets(parsed.get("added_points"), bullet_limit),
            "removed_points": _normalize_bullets(parsed.get("removed_points"), bullet_limit),
            "changed_points": _normalize_bullets(parsed.get("changed_points"), bullet_limit),
            "risk_notes": _normalize_bullets(parsed.get("risk_notes"), bullet_limit),
        }
    except Exception:
        logger.error("semantic_diff failed:\n%s", traceback.format_exc())
        raise


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
                        "OUTPUT CONTRACT — all six fields are required:\n"
                        "• resolution: exactly \"supersedes\" or \"merges\" (no other values allowed).\n"
                        "• updated_text: the merged or superseding text as described above.\n"
                        "• reason_summary: one sentence stating WHY this resolution was chosen.\n"
                        "• changed_claims: array of short strings, each naming one specific claim that changed or was added. Empty array if resolution is \"merges\" with no mutations.\n"
                        "• confidence_score: float 0.0–1.0 representing your certainty in the resolution. Use 1.0 for unambiguous factual contradictions, lower for inferred or ambiguous changes.\n"
                        "• evidence_used: one sentence explicitly citing which specific claims or phrases from OLD TEXT and NEW TEXT drove your resolution decision."
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
                            "reason_summary": {"type": "string"},
                            "changed_claims": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence_score": {"type": "number"},
                            "evidence_used": {"type": "string"},
                        },
                        "required": ["resolution", "updated_text", "reason_summary", "changed_claims", "confidence_score", "evidence_used"],
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
        if resolution not in ("supersedes", "merges"):
            resolution = "supersedes"
        updated_text = parsed.get("updated_text", new_text)
        reason_summary = parsed.get("reason_summary")
        changed_claims = parsed.get("changed_claims")
        confidence_score = parsed.get("confidence_score")
        evidence_used = parsed.get("evidence_used")
        result: ConflictResolutionResult = {"resolution": resolution, "updated_text": updated_text}
        if isinstance(reason_summary, str) and reason_summary.strip():
            result["reason_summary"] = reason_summary.strip()
        if isinstance(changed_claims, list):
            claims = [str(c).strip() for c in changed_claims if str(c).strip()]
            if claims:
                result["changed_claims"] = claims
        if isinstance(confidence_score, (int, float)):
            result["confidence_score"] = float(max(0.0, min(1.0, confidence_score)))
        if isinstance(evidence_used, str) and evidence_used.strip():
            result["evidence_used"] = evidence_used.strip()
        logger.debug(
            "Conflict resolved as '%s' (confidence=%.2f) with text length %d",
            resolution,
            result.get("confidence_score", -1.0),
            len(updated_text),
        )
        return result

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
