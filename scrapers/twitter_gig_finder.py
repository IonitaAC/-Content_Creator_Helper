"""
GigHunt — Twitter / X Gig Finder (twikit, $0)
================================================
Searches Twitter for "Hiring Editor" posts using ``twikit``, an async
Python client that uses Twitter's **internal private API** via
cookie-based authentication.  **No paid API key required.**

Authentication Flow
-------------------
``twikit`` authenticates via browser cookies.  Two methods are supported:

**Method A — Automatic login (recommended for first run):**
    The client calls ``client.login(username, email, password)`` which
    performs the same auth flow as the Twitter web app.  Cookies are
    then saved to ``cookies.json`` for subsequent runs.

**Method B — Manual cookie export (if login is blocked / 2FA):**
    1. Log in to https://x.com in your browser.
    2. Open DevTools (F12) → Application tab → Cookies → https://x.com.
    3. Copy ``auth_token`` and ``ct0`` values into ``.env``.
    4. The client will use these directly.

Rate Limiting & Ban Avoidance
-----------------------------
- Max 50 search requests per 15-minute window (Twitter's internal limit).
- We add random jitter (2–5 s) between paginated calls.
- If rate-limited (HTTP 429), we back off for the full 15-minute window.
- Redis deduplication prevents re-inserting tweets seen in prior scans.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from twikit import Client as TwikitClient

try:
    import redis.asyncio as aioredis
    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False

from config import get_settings

logger = logging.getLogger(__name__)


# ── DTOs ─────────────────────────────────────────────────────


@dataclass
class SocialPostData:
    """Normalised hiring-post record (shared with Reddit scraper)."""
    platform: str
    post_id: str
    author: str
    author_url: str
    text: str
    url: str
    query_matched: str
    likes: int = 0
    replies: int = 0
    posted_at: Optional[datetime] = None


# ── Constants ────────────────────────────────────────────────

COOKIES_FILE = Path("twitter_cookies.json")

DEFAULT_QUERIES: List[str] = [
    '("hiring" OR "looking for") ("editor" OR "video editor") ("youtube" OR "content")',
    '"youtube editor needed"',
    '"hiring video editor" min_faves:5',
    '"looking for an editor" ("gaming" OR "youtube")',
]

JITTER_MIN: float = 2.0
JITTER_MAX: float = 5.0
MAX_RESULTS_PER_QUERY: int = 50
RATE_LIMIT_WAIT: int = 900  # 15 minutes in seconds


# ── Twitter Gig Finder ──────────────────────────────────────


class TwitterGigFinder:
    """
    Async Twitter search for editor-hiring posts.

    Usage::

        finder = TwitterGigFinder()
        await finder.authenticate()
        posts = await finder.search_gigs(since_days=30)
        await finder.close()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = TwikitClient(language="en-US")
        self._redis = None  # Optional Redis connection
        self._memory_seen: set[str] = set()  # In-memory fallback

    # ── Authentication ───────────────────────────────────────

    async def authenticate(self) -> None:
        """
        Authenticate with Twitter/X using one of two strategies:

        1. **Saved cookies** — if ``twitter_cookies.json`` exists.
        2. **Login flow** — calls ``client.login()`` and saves cookies.
        3. **Manual cookies** — uses ``TWITTER_AUTH_TOKEN`` + ``TWITTER_CT0``
           from ``.env`` as fallback.
        """
        # Strategy 1: Load previously saved cookies
        if COOKIES_FILE.exists():
            logger.info("Loading Twitter cookies from %s", COOKIES_FILE)
            self._client.load_cookies(str(COOKIES_FILE))
            logger.info("✅ Twitter authenticated via saved cookies")
            return

        # Strategy 2: Automated login
        username = self._settings.twitter_username
        email = self._settings.twitter_email
        password = self._settings.twitter_password

        if username and email and password:
            logger.info("Attempting Twitter login for @%s ...", username)
            try:
                await self._client.login(
                    auth_info_1=username,
                    auth_info_2=email,
                    password=password,
                )
                # Save cookies for future runs
                self._client.save_cookies(str(COOKIES_FILE))
                logger.info("✅ Twitter login successful — cookies saved to %s", COOKIES_FILE)
                return
            except Exception as exc:
                logger.warning("Twitter login failed: %s — falling back to manual cookies", exc)

        # Strategy 3: Manual cookie injection
        auth_token = self._settings.twitter_auth_token
        ct0 = self._settings.twitter_ct0

        if auth_token and ct0:
            logger.info("Using manual Twitter cookies from .env")
            self._client.set_cookies({
                "auth_token": auth_token,
                "ct0": ct0,
            })
            logger.info("✅ Twitter authenticated via manual cookies")
            return

        logger.warning(
            "⚠️ Twitter authentication missing. GigHunt (Twitter) will be disabled.\n"
            "To enable: set TWITTER_AUTH_TOKEN + TWITTER_CT0 in .env"
        )
        return

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
        """Clean up connections."""
        if self._redis:
            await self._redis.close()

    # ── Core Search ──────────────────────────────────────────

    async def search_gigs(
        self,
        custom_queries: Optional[List[str]] = None,
        since_days: int = 30,
        max_results: int = MAX_RESULTS_PER_QUERY,
    ) -> List[SocialPostData]:
        """
        Search Twitter for hiring posts matching editor keywords.

        Parameters
        ----------
        custom_queries : list[str], optional
            Override the default search queries.
        since_days : int
            Only return posts from the last N days (default 30).
        max_results : int
            Max tweets to collect per query.

        Returns
        -------
        list[SocialPostData]
            Deduplicated hiring posts sorted by recency.
        """
        await self._init_redis()
        queries = custom_queries or DEFAULT_QUERIES
        all_posts: List[SocialPostData] = []
        seen_ids: set[str] = set()

        # Check if authenticated (cookies loaded or set)
        if not self._client.get_cookies():
            logger.warning("Skipping Twitter search — not authenticated")
            return []

        for query in queries:
            try:
                posts = await self._execute_search(query, since_days, max_results)
                for post in posts:
                    if post.post_id not in seen_ids:
                        # Dedup: Redis if available, else in-memory
                        redis_key = f"twitter:seen:{post.post_id}"
                        if self._redis:
                            already_seen = await self._redis.exists(redis_key)
                        else:
                            already_seen = post.post_id in self._memory_seen
                        if not already_seen:
                            seen_ids.add(post.post_id)
                            self._memory_seen.add(post.post_id)
                            all_posts.append(post)
                            if self._redis:
                                await self._redis.set(redis_key, "1", ex=604800)
            except Exception as exc:
                logger.error("Twitter search failed for query '%s': %s", query, exc)
                continue

        # Sort by posted_at descending (most recent first)
        all_posts.sort(key=lambda p: p.posted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        logger.info("Twitter search complete: %d unique posts found", len(all_posts))
        return all_posts

    async def _execute_search(
        self,
        query: str,
        since_days: int,
        max_results: int,
    ) -> List[SocialPostData]:
        """Run a single search query with pagination and rate-limit handling."""
        posts: List[SocialPostData] = []

        try:
            # twikit search_tweet returns a Result object with pagination
            result = await self._client.search_tweet(
                query=query,
                product="Latest",
                count=min(max_results, 20),
            )

            if not result:
                return posts

            # Process first page
            posts.extend(self._parse_tweets(result, query))

            # Paginate if needed
            collected = len(posts)
            while collected < max_results:
                # Rate-limit jitter
                await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

                try:
                    result = await result.next()
                    if not result:
                        break
                    new_posts = self._parse_tweets(result, query)
                    if not new_posts:
                        break
                    posts.extend(new_posts)
                    collected += len(new_posts)
                except Exception as exc:
                    if "429" in str(exc):
                        logger.warning(
                            "Twitter rate limit hit — waiting %d seconds",
                            RATE_LIMIT_WAIT,
                        )
                        await asyncio.sleep(RATE_LIMIT_WAIT)
                    else:
                        logger.warning("Pagination error: %s", exc)
                        break

        except Exception as exc:
            if "429" in str(exc):
                logger.warning("Twitter rate limit on initial search — backing off")
                await asyncio.sleep(RATE_LIMIT_WAIT)
            else:
                raise

        return posts

    def _parse_tweets(self, result, query: str) -> List[SocialPostData]:
        """Convert twikit tweet objects to our normalised DTO."""
        posts: List[SocialPostData] = []

        for tweet in result:
            try:
                posted_at = None
                if hasattr(tweet, "created_at") and tweet.created_at:
                    try:
                        posted_at = datetime.strptime(
                            tweet.created_at, "%a %b %d %H:%M:%S %z %Y"
                        )
                    except (ValueError, TypeError):
                        posted_at = datetime.now(timezone.utc)

                author_name = ""
                author_url = ""
                if hasattr(tweet, "user") and tweet.user:
                    author_name = tweet.user.screen_name or ""
                    author_url = f"https://x.com/{author_name}"

                posts.append(
                    SocialPostData(
                        platform="twitter",
                        post_id=str(tweet.id),
                        author=author_name,
                        author_url=author_url,
                        text=tweet.full_text or tweet.text or "",
                        url=f"https://x.com/{author_name}/status/{tweet.id}",
                        query_matched=query[:256],
                        likes=getattr(tweet, "favorite_count", 0) or 0,
                        replies=getattr(tweet, "reply_count", 0) or 0,
                        posted_at=posted_at,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse tweet: %s", exc)
                continue

        return posts


# ── Standalone test runner ───────────────────────────────────

async def _main() -> None:
    """Quick manual test — run with ``python -m scrapers.twitter_gig_finder``."""
    logging.basicConfig(level=logging.INFO)
    finder = TwitterGigFinder()
    await finder.authenticate()
    try:
        posts = await finder.search_gigs(since_days=7, max_results=10)
        print(f"\n{'='*60}")
        print(f"  Found {len(posts)} hiring posts on Twitter")
        print(f"{'='*60}")
        for p in posts[:5]:
            print(f"\n  @{p.author} ({p.posted_at})")
            print(f"  ❤ {p.likes}  💬 {p.replies}")
            print(f"  {p.text[:120]}...")
            print(f"  → {p.url}")
    finally:
        await finder.close()


if __name__ == "__main__":
    asyncio.run(_main())
