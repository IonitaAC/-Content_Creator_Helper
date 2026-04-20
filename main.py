"""
StreamScout & GigHunt — FastAPI Application
=============================================
RESTful API for the streamer discovery engine and gig finder.

Endpoints
---------
- ``GET  /api/streamers``        — Paginated list with filters
- ``GET  /api/streamers/{id}``   — Detail view
- ``PATCH /api/leads/{id}``      — Update lead status & notes
- ``POST /api/scan/trigger``     — Manually trigger Twitch scan
- ``GET  /api/gigs``             — GigHunt feed
- ``POST /api/gigs/search``      — On-demand Twitter/Reddit search
- ``GET  /health``               — Healthcheck

Run locally::

    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import get_settings
from database import AsyncSessionLocal, get_db, init_db
from models import (
    Lead,
    LeadStatus,
    Platform,
    SocialPost,
    Streamer,
    YouTubeChannel,
    YouTubeStatus,
)

logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables on startup."""
    logger.info("Initialising database...")
    await init_db()
    logger.info("✅ Database ready")
    yield


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="StreamScout & GigHunt",
    description="Zero-cost streamer discovery engine and editor gig finder.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Schemas ─────────────────────────────────────────


class StreamerResponse(BaseModel):
    id: int
    twitch_id: str
    login: str
    display_name: str
    profile_image_url: Optional[str] = None
    avg_viewers: int
    follower_count: int
    game_name: Optional[str] = None
    youtube_status: str
    has_clippers: bool
    first_seen_at: datetime
    last_scanned_at: datetime

    class Config:
        from_attributes = True


class StreamerDetailResponse(StreamerResponse):
    youtube_channels: List[YouTubeChannelResponse] = []
    lead: Optional[LeadResponse] = None


class YouTubeChannelResponse(BaseModel):
    id: int
    channel_id: str
    title: str
    subscriber_count: Optional[int] = None
    last_upload_date: Optional[datetime] = None
    is_official: bool
    is_clipper: bool
    confidence_score: float
    checked_at: datetime

    class Config:
        from_attributes = True


class LeadResponse(BaseModel):
    id: int
    streamer_id: int
    status: str
    notes: Optional[str] = None
    estimated_monthly_revenue: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LeadUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, description="New lead status")
    notes: Optional[str] = Field(None, description="Notes about the lead")
    estimated_monthly_revenue: Optional[float] = None


class GigPostResponse(BaseModel):
    id: int
    platform: str
    post_id: str
    author: str
    author_url: Optional[str] = None
    text: str
    url: str
    query_matched: str
    likes: int
    replies: int
    posted_at: datetime
    discovered_at: datetime

    class Config:
        from_attributes = True


class GigSearchRequest(BaseModel):
    platforms: List[str] = Field(default=["twitter", "reddit"])
    timeframe: str = Field(default="month")
    custom_queries: Optional[List[str]] = None


class ScanTriggerResponse(BaseModel):
    task_id: str
    status: str
    message: str


class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
    total_pages: int


# Forward refs for nested models
StreamerDetailResponse.model_rebuild()


# ── Routes: Streamers ───────────────────────────────────────

@app.get("/api/streamers", response_model=PaginatedResponse, tags=["Streamers"])
async def list_streamers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    youtube_status: Optional[str] = Query(None, description="Filter by YouTube status"),
    min_viewers: Optional[int] = Query(None, ge=0),
    min_followers: Optional[int] = Query(None, ge=0),
    has_clippers: Optional[bool] = Query(None),
    sort_by: str = Query("avg_viewers", description="Sort field"),
    sort_order: str = Query("desc", description="asc or desc"),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated list of discovered streamers with filtering and sorting.
    """
    query = select(Streamer)

    # ── Filters ──
    if youtube_status:
        try:
            yt_enum = YouTubeStatus(youtube_status)
            query = query.where(Streamer.youtube_status == yt_enum)
        except ValueError:
            raise HTTPException(400, f"Invalid youtube_status: {youtube_status}")

    if min_viewers is not None:
        query = query.where(Streamer.avg_viewers >= min_viewers)
    if min_followers is not None:
        query = query.where(Streamer.follower_count >= min_followers)
    if has_clippers is not None:
        query = query.where(Streamer.has_clippers == has_clippers)

    # ── Count ──
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # ── Sort ──
    sort_column = getattr(Streamer, sort_by, Streamer.avg_viewers)
    if sort_order == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # ── Paginate ──
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    streamers = result.scalars().all()

    return PaginatedResponse(
        items=[StreamerResponse.model_validate(s) for s in streamers],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


@app.get("/api/streamers/{streamer_id}", response_model=StreamerDetailResponse, tags=["Streamers"])
async def get_streamer(
    streamer_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get detailed streamer info including YouTube channels and lead status."""
    query = (
        select(Streamer)
        .where(Streamer.id == streamer_id)
        .options(
            selectinload(Streamer.youtube_channels),
            selectinload(Streamer.lead),
        )
    )
    result = await db.execute(query)
    streamer = result.scalar_one_or_none()

    if not streamer:
        raise HTTPException(404, f"Streamer {streamer_id} not found")

    return StreamerDetailResponse.model_validate(streamer)


# ── Routes: Leads ────────────────────────────────────────────

@app.patch("/api/leads/{lead_id}", response_model=LeadResponse, tags=["Leads"])
async def update_lead(
    lead_id: int,
    update: LeadUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update a lead's status, notes, or revenue estimate."""
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()

    if not lead:
        raise HTTPException(404, f"Lead {lead_id} not found")

    if update.status is not None:
        try:
            lead.status = LeadStatus(update.status)
        except ValueError:
            raise HTTPException(400, f"Invalid status: {update.status}")

    if update.notes is not None:
        lead.notes = update.notes

    if update.estimated_monthly_revenue is not None:
        lead.estimated_monthly_revenue = update.estimated_monthly_revenue

    await db.commit()
    await db.refresh(lead)

    return LeadResponse.model_validate(lead)


# ── Routes: Scan Trigger ────────────────────────────────────

@app.post("/api/scan/trigger", response_model=ScanTriggerResponse, tags=["Scanning"])
async def trigger_scan(
    max_pages: int = Query(10, ge=1, le=50),
):
    """
    Manually trigger a Twitch scan via Celery.

    Returns a task_id that can be used to check progress.
    """
    from tasks import run_daily_twitch_scan

    task = run_daily_twitch_scan.delay(max_pages=max_pages)

    return ScanTriggerResponse(
        task_id=task.id,
        status="queued",
        message=f"Twitch scan queued (max_pages={max_pages}). Task ID: {task.id}",
    )


# ── Routes: Gig Finder ──────────────────────────────────────

@app.get("/api/gigs", response_model=PaginatedResponse, tags=["GigHunt"])
async def list_gigs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    platform: Optional[str] = Query(None, description="twitter or reddit"),
    sort_order: str = Query("desc", description="asc or desc by posted_at"),
    db: AsyncSession = Depends(get_db),
):
    """Paginated feed of discovered hiring posts."""
    query = select(SocialPost)

    if platform:
        try:
            plat_enum = Platform(platform)
            query = query.where(SocialPost.platform == plat_enum)
        except ValueError:
            raise HTTPException(400, f"Invalid platform: {platform}")

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Sort
    if sort_order == "asc":
        query = query.order_by(SocialPost.posted_at.asc())
    else:
        query = query.order_by(SocialPost.posted_at.desc())

    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    posts = result.scalars().all()

    return PaginatedResponse(
        items=[GigPostResponse.model_validate(p) for p in posts],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


@app.post("/api/gigs/search", response_model=ScanTriggerResponse, tags=["GigHunt"])
async def search_gigs(
    request: GigSearchRequest,
):
    """
    Trigger an on-demand gig search via Celery.

    Returns a task_id for progress tracking.
    """
    from tasks import run_gig_search

    task = run_gig_search.delay(
        platforms=request.platforms,
        timeframe=request.timeframe,
        custom_queries=request.custom_queries,
    )

    return ScanTriggerResponse(
        task_id=task.id,
        status="queued",
        message=f"Gig search queued ({', '.join(request.platforms)}). Task ID: {task.id}",
    )


# ── Health ───────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def healthcheck():
    """Simple health check endpoint."""
    return {
        "status": "healthy",
        "service": "StreamScout & GigHunt",
        "version": "1.0.0",
    }


# ── API Status ────────────────────────────────────────────────

@app.get("/api/status", tags=["System"])
async def api_status():
    """Check which API integrations are configured."""
    s = get_settings()

    def is_set(val: str) -> bool:
        return bool(val) and not val.startswith("your_")

    return {
        "twitch": {
            "configured": is_set(s.twitch_client_id) and is_set(s.twitch_client_secret),
            "label": "Twitch API",
            "debug_id_prefix": s.twitch_client_id[:6] if s.twitch_client_id else "(empty)",
        },
        "youtube": {
            "configured": is_set(s.youtube_api_key),
            "label": "YouTube API",
            "debug_key_prefix": s.youtube_api_key[:6] if s.youtube_api_key else "(empty)",
        },
        "twitter": {
            "configured": is_set(s.twitter_auth_token),
            "label": "Twitter Scraper",
        },
        "reddit": {
            "configured": is_set(s.reddit_client_id),
            "label": "Reddit API",
        },
    }


# ── Create Lead ──────────────────────────────────────────────

@app.post("/api/leads", response_model=LeadResponse, tags=["Leads"])
async def create_lead(
    streamer_id: int = Query(..., description="Streamer ID to save as prospect"),
    db: AsyncSession = Depends(get_db),
):
    """Save a streamer as a lead / prospect."""
    streamer = (
        await db.execute(select(Streamer).where(Streamer.id == streamer_id))
    ).scalar_one_or_none()
    if not streamer:
        raise HTTPException(404, f"Streamer {streamer_id} not found")

    existing = (
        await db.execute(select(Lead).where(Lead.streamer_id == streamer_id))
    ).scalar_one_or_none()
    if existing:
        return LeadResponse.model_validate(existing)

    lead = Lead(streamer_id=streamer_id, status=LeadStatus.NEW_LEAD)
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return LeadResponse.model_validate(lead)


# ── SSE: Scan Stream ─────────────────────────────────────────

@app.get("/api/scan/stream", tags=["Scanning"])
async def stream_scan(max_pages: int = Query(10, ge=1, le=50)):
    """SSE endpoint — streams real-time scan progress to the Activity Island."""

    async def generate():
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        yield sse({"type": "status", "message": "Initializing scan engine…", "percent": 5})
        await asyncio.sleep(0.3)

        settings = get_settings()
        if not settings.twitch_client_id or settings.twitch_client_id.startswith("your_"):
            yield sse({
                "type": "error",
                "message": "Twitch API not configured. Add TWITCH_CLIENT_ID & SECRET to .env",
            })
            return

        yield sse({"type": "status", "message": "Connecting to Twitch API…", "percent": 10})

        try:
            from services.cross_reference import CrossReferencePipeline

            async with AsyncSessionLocal() as session:
                yield sse({
                    "type": "status",
                    "message": f"Running pipeline (max {max_pages} pages)…",
                    "percent": 25,
                })
                pipeline = CrossReferencePipeline(session)
                results = await pipeline.run(max_pages=max_pages)

                yield sse({
                    "type": "complete",
                    "message": f"Scan complete! Processed {len(results)} streamers.",
                    "percent": 100,
                    "count": len(results),
                })
        except Exception as exc:
            yield sse({"type": "error", "message": f"Scan failed: {exc}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── SSE: Gig Search Stream ───────────────────────────────────

@app.get("/api/gigs/search/stream", tags=["GigHunt"])
async def stream_gig_search(
    platforms: str = Query("twitter,reddit"),
    timeframe: str = Query("month"),
):
    """SSE endpoint — streams gig search progress."""

    async def generate():
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        platform_list = [p.strip() for p in platforms.split(",")]
        yield sse({
            "type": "status",
            "message": f"Starting gig search on {', '.join(platform_list)}…",
            "percent": 10,
        })
        await asyncio.sleep(0.3)

        settings = get_settings()
        found_total = 0

        for i, platform in enumerate(platform_list):
            pct = 10 + int(((i + 1) / len(platform_list)) * 70)

            if platform == "twitter":
                if not settings.twitter_auth_token or settings.twitter_auth_token.startswith("your_"):
                    yield sse({"type": "log", "message": "[WARN] Twitter not configured — skipping"})
                    continue
                yield sse({"type": "status", "message": "Searching Twitter / X for gigs…", "percent": pct})
                try:
                    from scrapers.twitter_gig_finder import TwitterGigFinder
                    finder = TwitterGigFinder()
                    await finder.authenticate()
                    posts = await finder.search_gigs(since_days=_timeframe_days(timeframe))
                    found_total += len(posts)
                    await finder.close()
                except Exception as exc:
                    yield sse({"type": "log", "message": f"[WARN] Twitter search error: {exc}"})

            elif platform == "reddit":
                if not settings.reddit_client_id or settings.reddit_client_id.startswith("your_"):
                    yield sse({"type": "log", "message": "[WARN] Reddit not configured — skipping"})
                    continue
                yield sse({"type": "status", "message": "Searching Reddit for gigs…", "percent": pct})
                try:
                    from scrapers.reddit_gig_finder import RedditGigFinder
                    finder = RedditGigFinder()
                    await finder.connect()
                    posts = await finder.search_gigs(timeframe=timeframe)
                    found_total += len(posts)
                    await finder.close()
                except Exception as exc:
                    yield sse({"type": "log", "message": f"[WARN] Reddit search error: {exc}"})

        yield sse({
            "type": "complete",
            "message": f"Search complete! Found {found_total} posts.",
            "percent": 100,
            "count": found_total,
        })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _timeframe_days(tf: str) -> int:
    return {"week": 7, "month": 30, "3months": 90, "6months": 180}.get(tf, 30)


# ── Dashboard & Static Files ─────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_dashboard():
    """Serve the Command Center dashboard."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── Entrypoint ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
