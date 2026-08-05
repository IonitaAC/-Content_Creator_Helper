"""
GigHunt — Reddit Gig Finder (asyncpraw, Free Tier)
=====================================================
Searches targeted subreddits for "Hiring Editor" posts using
``asyncpraw`` (Async Python Reddit API Wrapper).

Reddit API Limits (Free "script" App)
-------------------------------------
- 60 requests / minute (enforced by asyncpraw internally).
- 100 listings per request (max).
- We add manual sleep on 429 as an extra safety net.

Target Subreddits
-----------------
r/forhire, r/editors, r/youtubers, r/CreatorServices,
r/VideoEditing, r/gaming, r/Twitch, r/NewTubers
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import asyncpraw

try:
    import redis.asyncio as aioredis
    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False

from config import get_settings

logger = logging.getLogger(__name__)


# ── DTOs ─────────────────────────────────────────────────────

# Re-use SocialPostData from the twitter module for consistency
from scrapers.twitter_gig_finder import SocialPostData


# ── Constants ────────────────────────────────────────────────

TARGET_SUBREDDITS: List[str] = [
    "forhire",
    "editors",
    "youtubers",
    "CreatorServices",
    "VideoEditing",
    "gaming",
    "Twitch",
    "NewTubers",
    "editingandlayout",
]

DEFAULT_QUERIES: List[str] = [
    "hiring editor",
    "looking for editor",
    "youtube editor needed",
    "video editor needed",
    "hiring video editor",
    "looking for video editor",
    "need an editor",
]

# Reddit time filter mappings
TIME_FILTER_MAP = {
    "week": "week",
    "month": "month",
    "3months": "year",    # Reddit only supports week/month/year/all
    "6months": "year",    # We'll post-filter by date
    "year": "year",
    "all": "all",
}

RATE_LIMIT_SLEEP: float = 2.0  # Extra safety between subreddits


# ── Reddit Gig Finder ───────────────────────────────────────


class RedditGigFinder:
    """
    Async Reddit search for editor-hiring posts.

    Usage::

        finder = RedditGigFinder()
        await finder.connect()
        posts = await finder.search_gigs(timeframe="month")
        await finder.close()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._reddit: Optional[asyncpraw.Reddit] = None
        self._redis = None  # Optional Redis connection
        self._memory_seen: set[str] = set()  # In-memory fallback

    # ── Connection ───────────────────────────────────────────

    async def connect(self) -> None:
        """Initialise the asyncpraw Reddit client."""
        if not self._settings.reddit_client_id or not self._settings.reddit_client_secret:
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in .env. "
                "GigHunt (Reddit) will be disabled."
            )
            return

        self._reddit = asyncpraw.Reddit(
            client_id=self._settings.reddit_client_id,
            client_secret=self._settings.reddit_client_secret,
            user_agent=self._settings.reddit_user_agent,
        )
        logger.info("✅ Connected to Reddit API (asyncpraw, read-only)")

    async def _init_redis(self) -> None:
        """Try to connect to Redis for deduplication; skip if unavailable."""
        if self._redis is not None:
            return
        if not _HAS_REDIS or not self._settings.redis_url:
            logger.info("Redis not configured — using in-memory deduplication")
            return
        try:
            self._redis = aioredis.from_url(
                self._settings.redis_url,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("✅ Redis connected for deduplication")
        except Exception as exc:
            logger.warning("Redis unavailable (%s) — using in-memory dedup", exc)
            self._redis = None

    async def close(self) -> None:
        """Clean up all connections."""
        if self._reddit:
            await self._reddit.close()
        if self._redis:
            await self._redis.close()

    # ── Core Search ──────────────────────────────────────────

    async def search_gigs(
        self,
        custom_queries: Optional[List[str]] = None,
        subreddits: Optional[List[str]] = None,
        timeframe: str = "month",
        max_results_per_sub: int = 25,
    ) -> List[SocialPostData]:
        """
        Search Reddit for hiring posts across target subreddits.

        Parameters
        ----------
        custom_queries : list[str], optional
            Override default search queries.
        subreddits : list[str], optional
            Override default target subreddits.
        timeframe : str
            One of 'week', 'month', '3months', '6months', 'year', 'all'.
        max_results_per_sub : int
            Max posts per subreddit per query.

        Returns
        -------
        list[SocialPostData]
            Deduplicated posts sorted by recency.
        """
        if self._reddit is None:
            logger.warning("Skipping Reddit search — not authenticated")
            return []

        await self._init_redis()

        queries = custom_queries or DEFAULT_QUERIES
        subs = subreddits or TARGET_SUBREDDITS
        reddit_time = TIME_FILTER_MAP.get(timeframe, "month")

        all_posts: List[SocialPostData] = []
        seen_ids: set[str] = set()

        for subreddit_name in subs:
            for query in queries:
                try:
                    posts = await self._search_subreddit(
                        subreddit_name=subreddit_name,
                        query=query,
                        time_filter=reddit_time,
                        limit=max_results_per_sub,
                    )

                    for post in posts:
                        if post.post_id in seen_ids:
                            continue

                        # Dedup: Redis if available, else in-memory
                        redis_key = f"reddit:seen:{post.post_id}"
                        if self._redis:
                            already_seen = await self._redis.exists(redis_key)
                        else:
                            already_seen = post.post_id in self._memory_seen
                        if already_seen:
                            continue

                        # Post-filter for 3months / 6months
                        if timeframe in ("3months", "6months") and post.posted_at:
                            months = 3 if timeframe == "3months" else 6
                            cutoff = datetime.now(timezone.utc).timestamp() - (months * 30 * 86400)
                            if post.posted_at.timestamp() < cutoff:
                                continue

                        seen_ids.add(post.post_id)
                        self._memory_seen.add(post.post_id)
                        all_posts.append(post)

                        if self._redis:
                            await self._redis.set(redis_key, "1", ex=604800)

                except Exception as exc:
                    logger.warning(
                        "Reddit search failed for r/%s query='%s': %s",
                        subreddit_name, query, exc,
                    )
                    continue

            # Rate-limit safety between subreddits
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        # Sort by posted_at descending
        all_posts.sort(
            key=lambda p: p.posted_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        logger.info(
            "Reddit search complete: %d unique posts across %d subreddits",
            len(all_posts), len(subs),
        )
        return all_posts

    async def _search_subreddit(
        self,
        subreddit_name: str,
        query: str,
        time_filter: str,
        limit: int,
    ) -> List[SocialPostData]:
        """Search a single subreddit for matching posts."""
        posts: List[SocialPostData] = []

        try:
            subreddit = await self._reddit.subreddit(subreddit_name)
            async for submission in subreddit.search(
                query=query,
                time_filter=time_filter,
                limit=limit,
                sort="new",
            ):
                try:
                    posted_at = datetime.fromtimestamp(
                        submission.created_utc, tz=timezone.utc
                    )

                    # Combine title + selftext for the content
                    text = submission.title
                    if submission.selftext:
                        text += f"\n\n{submission.selftext[:500]}"

                    posts.append(
                        SocialPostData(
                            platform="reddit",
                            post_id=submission.id,
                            author=str(submission.author) if submission.author else "[deleted]",
                            author_url=(
                                f"https://reddit.com/u/{submission.author}"
                                if submission.author else ""
                            ),
                            text=text,
                            url=f"https://reddit.com{submission.permalink}",
                            query_matched=query,
                            likes=submission.score,
                            replies=submission.num_comments,
                            posted_at=posted_at,
                        )
                    )
                except Exception as exc:
                    logger.warning("Failed to parse Reddit post: %s", exc)
                    continue

        except Exception as exc:
            # Handle 403 (private sub), 429 (rate limit), etc.
            exc_str = str(exc)
            if "429" in exc_str:
                logger.warning("Reddit rate limit hit on r/%s — sleeping 60s", subreddit_name)
                await asyncio.sleep(60)
            elif "403" in exc_str or "private" in exc_str.lower():
                logger.info("r/%s is private/restricted — skipping", subreddit_name)
            else:
                raise

        return posts


# ── Standalone test runner ───────────────────────────────────

async def _main() -> None:
    """Quick manual test — run with ``python -m scrapers.reddit_gig_finder``."""
    logging.basicConfig(level=logging.INFO)
    finder = RedditGigFinder()
    await finder.connect()
    try:
        posts = await finder.search_gigs(timeframe="month", max_results_per_sub=5)
        print(f"\n{'='*60}")
        print(f"  Found {len(posts)} hiring posts on Reddit")
        print(f"{'='*60}")
        for p in posts[:10]:
            print(f"\n  u/{p.author} ({p.posted_at})")
            print(f"  ⬆ {p.likes}  💬 {p.replies}")
            print(f"  {p.text[:150]}...")
            print(f"  → {p.url}")
    finally:
        await finder.close()


if __name__ == "__main__":
    asyncio.run(_main())
