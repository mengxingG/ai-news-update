#!/usr/bin/env python3
"""FastAPI layer: run last30days retrieval + ranking, return top items as Notion-ready JSON."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from lib import dates as last30_dates
from lib import env as last30_env
from lib import pipeline, schema

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

# Sources only (no X/Twitter, no web/grounding, etc.)
NEWS_SOURCES: list[str] = ["hackernews", "polymarket", "reddit", "youtube"]
ALLOWED_SOURCES: frozenset[str] = frozenset(NEWS_SOURCES)

SOURCE_LABELS: dict[str, str] = {
    "hackernews": "Hacker News",
    "polymarket": "Polymarket",
    "reddit": "Reddit",
    "youtube": "YouTube",
}

app = FastAPI(title="last30days news API", version="1.0.0")


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
        report = pipeline.run(
            topic=topic,
            config=config,
            depth="default",
            requested_sources=list(NEWS_SOURCES),
            mock=False,
            web_backend="none",
            lookback_days=days,
        )
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"pipeline failed: {exc}") from exc
    return JSONResponse(content=rows)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
