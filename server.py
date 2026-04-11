#!/usr/bin/env python3
"""FastAPI layer: run last30days retrieval + ranking, return top items as Notion-ready JSON."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from lib import dates as last30_dates
from lib import env as last30_env
from lib import pipeline, schema

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-news-update")

GEMINI_HTTP_TIMEOUT_SEC = 25
GEMINI_BATCH_DEADLINE_SEC = 90
GEMINI_POOL_WORKERS = 3

# ---------------------------------------------------------------------------
# Env bootstrap: project-root `.env` / `.env.local` (last30days core uses
# `~/.config/last30days/.env` and `.claude/last30days.env` unless vars exist).
# ---------------------------------------------------------------------------


def _merge_project_dotenv() -> None:
    from lib.env import load_env_file

    merged: dict[str, str] = {}
    for name in (".env", ".env.local"):
        path = ROOT / name
        if path.exists():
            merged.update(load_env_file(path))
    for key, val in merged.items():
        if val is not None and str(val).strip() != "":
            os.environ.setdefault(key, str(val))


_merge_project_dotenv()

_llm_provider_raw = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
if _llm_provider_raw in ("gemini", "deepseek"):
    LLM_PROVIDER = _llm_provider_raw
else:
    if _llm_provider_raw:
        log.warning("Unknown LLM_PROVIDER=%r, using gemini", _llm_provider_raw)
    LLM_PROVIDER = "gemini"

# Sources only (no X/Twitter, no web/grounding, no Reddit, etc.)
NEWS_SOURCES: list[str] = ["hackernews", "polymarket", "youtube"]
ALLOWED_SOURCES: frozenset[str] = frozenset(NEWS_SOURCES)

SOURCE_LABELS: dict[str, str] = {
    "hackernews": "Hacker News",
    "polymarket": "Polymarket",
    "youtube": "YouTube",
}

GEMINI_MODEL_ID = "gemini-2.5-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
_gemini_model: Any = None
_openai_client: Any = None


def _init_llm() -> None:
    """Configure translation LLM: Gemini (REST) or DeepSeek (OpenAI-compatible)."""
    global _gemini_model, _openai_client
    _gemini_model = None
    _openai_client = None

    if LLM_PROVIDER == "deepseek":
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            log.warning("LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY not set; translation degraded")
            return
        try:
            from openai import OpenAI

            _openai_client = OpenAI(
                api_key=key,
                base_url=DEEPSEEK_BASE_URL,
                timeout=GEMINI_HTTP_TIMEOUT_SEC,
            )
            log.info(
                "DeepSeek client ready (base_url=%s, model=%s)",
                DEEPSEEK_BASE_URL,
                DEEPSEEK_MODEL,
            )
        except ImportError:
            log.warning("openai package not installed; pip install openai (required for DeepSeek)")
        except Exception as exc:
            log.warning("DeepSeek init failed: %s", exc)
        return

    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        log.warning("GEMINI_API_KEY not set; Chinese title/summary will be skipped (degraded)")
        return
    try:
        genai.configure(api_key=key, transport="rest")
        _gemini_model = genai.GenerativeModel(GEMINI_MODEL_ID)
        log.info("Gemini model ready: %s (transport=rest)", GEMINI_MODEL_ID)
    except Exception as exc:
        log.warning("Gemini init failed: %s", exc)
        _gemini_model = None


def _llm_ready() -> bool:
    return _gemini_model is not None or _openai_client is not None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _init_llm()
    yield


app = FastAPI(title="last30days news API", version="1.0.0", lifespan=_lifespan)


def _format_date_yyyy_mm_dd(value: str | None) -> str:
    if not value or not str(value).strip():
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return s[:10] if len(s) >= 10 else s


def _original_text(candidate: schema.Candidate) -> str:
    parts: list[str] = []
    if (candidate.explanation or "").strip():
        parts.append(candidate.explanation.strip())
    if (candidate.snippet or "").strip():
        parts.append(candidate.snippet.strip())
    primary = schema.candidate_primary_item(candidate)
    if primary:
        why = (primary.why_relevant or "").strip()
        if why and why not in "\n\n".join(parts):
            parts.append(why)
        meta = primary.metadata or {}
        insights = meta.get("comment_insights")
        if isinstance(insights, list) and insights:
            lines: list[str] = []
            for item in insights[:5]:
                if isinstance(item, str) and item.strip():
                    lines.append(item.strip())
                elif isinstance(item, dict):
                    t = (item.get("text") or item.get("excerpt") or "").strip()
                    if t:
                        lines.append(t)
            if lines:
                parts.append("Highlights:\n" + "\n".join(f"• {ln}" for ln in lines))
        if not parts and (primary.body or "").strip():
            parts.append((primary.body or "").strip()[:4000])
    return "\n\n".join(parts)[:12000]


def _author_for(candidate: schema.Candidate) -> str:
    primary = schema.candidate_primary_item(candidate)
    if not primary:
        return "Unknown"
    author = (primary.author or "").strip()
    if author:
        return author
    container = (primary.container or "").strip()
    if container:
        return container
    return "Unknown"


def _candidate_to_row(candidate: schema.Candidate) -> dict[str, str]:
    src_key = (candidate.source or "").strip()
    if not src_key:
        srcs = schema.candidate_sources(candidate)
        src_key = srcs[0] if srcs else ""
    label = SOURCE_LABELS.get(src_key, src_key.replace("_", " ").title() if src_key else "Unknown")
    return {
        "Title": (candidate.title or "").strip(),
        "Source": label,
        "Author": _author_for(candidate),
        "OriginalText": _original_text(candidate),
        "URL": (candidate.url or "").strip(),
        "Date": _format_date_yyyy_mm_dd(schema.candidate_best_published_at(candidate)),
    }


def _gemini_failure_row(row: dict[str, str]) -> dict[str, str]:
    """Keep English title; mark translation failure."""
    out = dict(row)
    out["Title"] = (row.get("Title") or "").strip()
    out["OriginalText"] = "摘要生成失败"
    return out


def _build_translation_prompt(english_title: str, excerpt: str) -> str:
    title = (english_title or "").strip() or "(untitled)"
    text = (excerpt or "").strip() or "(no excerpt)"
    return (
        "You translate and summarize for a tech/AI reader. "
        "Reply with a single JSON object ONLY, no markdown, keys exactly:\n"
        '{"title_cn": "...", "summary_cn": "..."}\n'
        "- title_cn: Chinese headline, at most 20 Chinese characters, no line breaks.\n"
        "- summary_cn: Chinese summary, between 150 and 200 Chinese characters (inclusive), "
        "focus on AI/technology; no line breaks.\n\n"
        f"English title:\n{title}\n\n"
        f"Excerpt / body:\n{text[:8000]}\n"
    )


def _parse_title_summary_json(raw: str) -> tuple[str, str] | None:
    try:
        data = json.loads(raw.strip())
        title_cn = str(data.get("title_cn", "")).strip().replace("\n", " ")
        summary_cn = str(data.get("summary_cn", "")).strip().replace("\n", " ")
        if not title_cn or not summary_cn:
            return None
        if len(title_cn) > 20:
            title_cn = title_cn[:20]
        if len(summary_cn) > 200:
            summary_cn = summary_cn[:200]
        return title_cn, summary_cn
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return None


def _llm_fetch_title_summary_json(english_title: str, excerpt: str) -> tuple[str, str] | None:
    """Call active provider; returns (title_cn, summary_cn) or None."""
    prompt = _build_translation_prompt(english_title, excerpt)

    if LLM_PROVIDER == "deepseek":
        if _openai_client is None:
            return None
        try:
            kwargs: dict[str, Any] = {
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.35,
            }
            try:
                comp = _openai_client.chat.completions.create(
                    **kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception:
                comp = _openai_client.chat.completions.create(**kwargs)
            raw = (comp.choices[0].message.content or "").strip()
            return _parse_title_summary_json(raw)
        except Exception:
            return None

    if _gemini_model is None:
        return None
    try:
        resp = _gemini_model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.35,
                response_mime_type="application/json",
            ),
            request_options={"timeout": GEMINI_HTTP_TIMEOUT_SEC},
        )
        try:
            raw = (resp.text or "").strip()
        except ValueError:
            return None
        return _parse_title_summary_json(raw)
    except Exception:
        return None


def _gemini_enrich_one_indexed(item_index: int, total: int, row: dict[str, str]) -> dict[str, str]:
    """Single row: progress logs, SDK HTTP timeout, isolated failures."""
    log.info("[LLM] item %s/%s start (%s)", item_index, total, LLM_PROVIDER)
    t0 = time.perf_counter()
    title_en = (row.get("Title") or "").strip()
    excerpt = (row.get("OriginalText") or "").strip()

    if not _llm_ready():
        log.warning("[LLM] item %s/%s failed: provider not configured", item_index, total)
        return _gemini_failure_row(row)

    try:
        pair = _llm_fetch_title_summary_json(title_en, excerpt)
        if pair is None:
            log.warning(
                "[LLM] item %s/%s failed: empty or unparseable response",
                item_index,
                total,
            )
            return _gemini_failure_row(row)
        title_cn, summary_cn = pair
        out = dict(row)
        out["Title"] = title_cn
        out["OriginalText"] = summary_cn
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log.info("[LLM] item %s/%s done (%sms)", item_index, total, elapsed_ms)
        return out
    except Exception as exc:
        log.warning("[LLM] item %s/%s failed: %s", item_index, total, exc)
        return _gemini_failure_row(row)


def _apply_gemini_to_out(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows:
        return rows
    n = len(rows)
    deadline = time.monotonic() + GEMINI_BATCH_DEADLINE_SEC
    results: dict[int, dict[str, str]] = {}

    with ThreadPoolExecutor(max_workers=GEMINI_POOL_WORKERS) as pool:
        future_to_idx: dict[object, int] = {}
        for i, row in enumerate(rows):
            fut = pool.submit(_gemini_enrich_one_indexed, i + 1, n, row)
            future_to_idx[fut] = i

        pending = set(future_to_idx.keys())
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=min(remaining, 15.0),
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:
                    log.warning("[LLM] item %s/%s failed: %s", idx + 1, n, exc)
                    results[idx] = _gemini_failure_row(rows[idx])

        for fut in pending:
            idx = future_to_idx[fut]
            if idx not in results:
                log.warning(
                    "[LLM] item %s/%s skipped (batch deadline %ss exceeded)",
                    idx + 1,
                    n,
                    GEMINI_BATCH_DEADLINE_SEC,
                )
                results[idx] = _gemini_failure_row(rows[idx])

    return [results.get(i, _gemini_failure_row(rows[i])) for i in range(n)]


def _noop_supplemental(**_kwargs: object) -> None:
    """Skip pipeline Phase-2 X handle search (Bird) even when Reddit surfaces @handles."""
    return None


def _parse_window_bounds(days: int) -> tuple[date, date]:
    """Same inclusive window as `pipeline.run` via `dates.get_date_range(lookback_days)`."""
    from_str, to_str = last30_dates.get_date_range(days)
    from_d = datetime.strptime(from_str, "%Y-%m-%d").date()
    to_d = datetime.strptime(to_str, "%Y-%m-%d").date()
    return from_d, to_d


def _candidate_published_in_window(candidate: schema.Candidate, from_d: date, to_d: date) -> bool:
    raw = schema.candidate_best_published_at(candidate)
    if not raw:
        return False
    parsed = last30_dates.parse_date(raw)
    if parsed is None:
        return False
    d = parsed.date()
    return from_d <= d <= to_d


def run_news_for_topic(topic: str, *, days: int) -> list[dict[str, str]]:
    config = last30_env.get_config()
    from_d, to_d = _parse_window_bounds(days)
    real = pipeline._run_supplemental_searches
    pipeline._run_supplemental_searches = _noop_supplemental  # type: ignore[method-assign]
    try:
        log.info(
            "[pipeline] invoking pipeline.run topic=%r days=%s requested_sources=%s",
            topic,
            days,
            NEWS_SOURCES,
        )
        report = pipeline.run(
            topic=topic,
            config=config,
            depth="default",
            requested_sources=list(NEWS_SOURCES),
            mock=False,
            web_backend="none",
            lookback_days=days,
        )
        log.info("[pipeline] pipeline.run returned (%s ranked candidates)", len(report.ranked_candidates))
    finally:
        pipeline._run_supplemental_searches = real  # type: ignore[method-assign]

    out: list[dict[str, str]] = []
    for cand in report.ranked_candidates:
        if cand.source not in ALLOWED_SOURCES:
            continue
        if not _candidate_published_in_window(cand, from_d, to_d):
            continue
        out.append(_candidate_to_row(cand))
        if len(out) >= 15:
            break

    log.info("[LLM] start processing %s candidates (provider=%s)", len(out), LLM_PROVIDER)
    out = _apply_gemini_to_out(out)
    return out


@app.get("/api/news")
def api_news(
    topic: str = Query(..., min_length=1, description="Research keyword or phrase"),
    days: int = Query(
        1,
        ge=1,
        le=366,
        description="Lookback window in days (passed to pipeline lookback_days; same as CLI --lookback-days).",
    ),
) -> JSONResponse:
    q = topic.strip()
    if not q:
        raise HTTPException(status_code=400, detail="topic must be non-empty")
    try:
        rows = run_news_for_topic(q, days=days)
    except RuntimeError as exc:
        log.exception("pipeline RuntimeError for topic=%r", q)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        tb = traceback.format_exc()
        log.exception("/api/news failed topic=%r", q)
        raise HTTPException(
            status_code=500,
            detail=f"pipeline failed: {exc}\n\n{tb}",
        ) from exc
    return JSONResponse(content=rows)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
