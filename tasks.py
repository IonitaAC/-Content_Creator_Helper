from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from celery_app import celery_app
from config import get_settings

logger = logging.getLogger(__name__)


# ── Helper: Run async code in sync Celery worker ─────────────

def _run_async(coro):
    """Run an async coroutine in a sync context (Celery worker)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Task: Daily Twitch Scan ──────────────────────────────────

@celery_app.task(
    name="tasks.run_daily_twitch_scan",
    bind=True,
    max_retries=3,
    default_retry_delay=300,  # 5 minutes
)
def run_daily_twitch_scan(self, max_pages: int = 20) -> Dict[str, Any]:
    """
    Full StreamScout pipeline: Twitch → YouTube → DB.

    Called daily at 03:00 UTC by Celery Beat, or manually via
    ``POST /api/scan/trigger``.
    """
    logger.info("🚀 Starting daily Twitch scan (max_pages=%d)", max_pages)

    try:
        result = _run_async(_run_twitch_pipeline(max_pages))
        logger.info("✅ Daily scan complete: %d streamers processed", len(result))
        return {
            "status": "success",
            "streamers_processed": len(result),
            "results": result,
        }
    except Exception as exc:
        logger.error("❌ Daily scan failed: %s", exc)
        # Retry with exponential backoff
        raise self.retry(exc=exc)


async def _run_twitch_pipeline(max_pages: int) -> List[dict]:
    """Async inner function for the Twitch scan."""
    from database import AsyncSessionLocal
    from services.cross_reference import CrossReferencePipeline

    async with AsyncSessionLocal() as session:
        pipeline = CrossReferencePipeline(session)
        return await pipeline.run(max_pages=max_pages)


# ── Task: GigHunt Search ────────────────────────────────────

@celery_app.task(
    name="tasks.run_gig_search",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def run_gig_search(
    self,
    platforms: List[str] | None = None,
    timeframe: str = "month",
    custom_queries: List[str] | None = None,
) -> Dict[str, Any]:
    """
    Search Twitter + Reddit for hiring-editor posts.

    Called on-demand via ``POST /api/gigs/search``.
    """
    platforms = platforms or ["twitter", "reddit"]
    logger.info("🔍 GigHunt search: platforms=%s, timeframe=%s", platforms, timeframe)

    try:
        result = _run_async(
            _run_gig_pipeline(platforms, timeframe, custom_queries)
        )
        return {
            "status": "success",
            "total_posts": len(result),
            "posts": result,
        }
    except Exception as exc:
        logger.error("❌ Gig search failed: %s", exc)
        raise self.retry(exc=exc)


async def _run_gig_pipeline(
    platforms: List[str],
    timeframe: str,
    custom_queries: List[str] | None,
) -> List[dict]:
    """Async inner function for the gig search."""
    from database import AsyncSessionLocal
    from models import Platform, SocialPost

    all_posts: List[dict] = []

    # ── Twitter ──
    if "twitter" in platforms:
        try:
            from scrapers.twitter_gig_finder import TwitterGigFinder

            finder = TwitterGigFinder()
            await finder.authenticate()
            try:
                posts = await finder.search_gigs(
                    custom_queries=custom_queries,
                    since_days=_timeframe_to_days(timeframe),
                )
                for p in posts:
                    all_posts.append({
                        "platform": p.platform,
                        "post_id": p.post_id,
                        "author": p.author,
                        "text": p.text[:500],
                        "url": p.url,
                        "likes": p.likes,
                        "replies": p.replies,
                        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                    })
            finally:
                await finder.close()
        except Exception as exc:
            logger.error("Twitter search failed: %s", exc)

    # ── Reddit ──
    if "reddit" in platforms:
        try:
            from scrapers.reddit_gig_finder import RedditGigFinder

            finder = RedditGigFinder()
            await finder.connect()
            try:
                posts = await finder.search_gigs(
                    custom_queries=custom_queries,
                    timeframe=timeframe,
                )
                for p in posts:
                    all_posts.append({
                        "platform": p.platform,
                        "post_id": p.post_id,
                        "author": p.author,
                        "text": p.text[:500],
                        "url": p.url,
                        "likes": p.likes,
                        "replies": p.replies,
                        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                    })
            finally:
                await finder.close()
        except Exception as exc:
            logger.error("Reddit search failed: %s", exc)

    # ── Persist to DB ──
    async with AsyncSessionLocal() as session:
        for post_data in all_posts:
            from sqlalchemy import select

            # Check if already exists
            existing = await session.execute(
                select(SocialPost).where(
                    SocialPost.platform == Platform(post_data["platform"]),
                    SocialPost.post_id == post_data["post_id"],
                )
            )
            if existing.scalar_one_or_none():
                continue

            from datetime import datetime as dt

            posted_at = (
                dt.fromisoformat(post_data["posted_at"])
                if post_data["posted_at"]
                else dt.now()
            )

            social_post = SocialPost(
                platform=Platform(post_data["platform"]),
                post_id=post_data["post_id"],
                author=post_data["author"],
                text=post_data["text"],
                url=post_data["url"],
                query_matched=custom_queries[0] if custom_queries else "default",
                likes=post_data["likes"],
                replies=post_data["replies"],
                posted_at=posted_at,
            )
            session.add(social_post)

        await session.commit()

    logger.info("GigHunt pipeline complete: %d posts collected", len(all_posts))
    return all_posts


def _timeframe_to_days(timeframe: str) -> int:
    """Convert a timeframe string to number of days."""
    mapping = {
        "week": 7,
        "month": 30,
        "3months": 90,
        "6months": 180,
        "year": 365,
        "all": 3650,
    }
    return mapping.get(timeframe, 30)
