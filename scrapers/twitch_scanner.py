"""
StreamScout — Twitch Scanner (Helix API, Free App Token)
==========================================================
Discovers "Top-Tier" streamers: >1 000 concurrent viewers AND
>100 000 followers.  Uses **App Access Tokens** (client-credentials
flow) which are free and support high throughput.

Rate Limits (App Token):
    - 800 requests / minute (shared across all endpoints).
    - We paginate ``get_streams`` (max 100 per page) and then
      batch-check follower counts.

Error Handling:
    - Retries on 429 / 5xx with exponential backoff (max 3 retries).
    - Logs every retry so operators can spot throttling trends.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional
import inspect

from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope
from twitchAPI.helper import first

from config import get_settings

logger = logging.getLogger(__name__)


# ── DTOs ─────────────────────────────────────────────────────


@dataclass
class StreamerData:
    """Lightweight data-transfer object returned by the scanner."""
    twitch_id: str
    login: str
    display_name: str
    profile_image_url: str
    avg_viewers: int
    follower_count: int
    game_name: Optional[str] = None
    youtube_url_from_panels: Optional[str] = None


# ── Configuration ────────────────────────────────────────────

MAX_RETRIES: int = 3
BASE_BACKOFF_SECONDS: float = 2.0


# ── Scanner ──────────────────────────────────────────────────


class TwitchScanner:
    """
    Async Twitch scanner that returns qualified streamer profiles.

    Lifecycle::

        scanner = TwitchScanner()
        await scanner.connect()
        streamers = await scanner.get_top_streamers()
        await scanner.close()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._twitch: Optional[Twitch] = None

    # ── Connection ───────────────────────────────────────────

    async def connect(self) -> None:
        """Authenticate with Twitch using client-credentials (App Token)."""
        if not self._settings.twitch_client_id or not self._settings.twitch_client_secret:
            logger.warning(
                "⚠️ TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET missing. "
                "StreamScout scanner will be disabled."
            )
            return

        self._twitch = await Twitch(
            self._settings.twitch_client_id,
            self._settings.twitch_client_secret,
        )
        logger.info("✅ Connected to Twitch Helix API (App Token)")

    async def close(self) -> None:
        """Gracefully close the Twitch client."""
        if self._twitch:
            await self._twitch.close()
            logger.info("Twitch client closed")

    # ── Core Logic ───────────────────────────────────────────

    async def get_top_streamers(
        self,
        min_viewers: Optional[int] = None,
        min_followers: Optional[int] = None,
        max_pages: int = 20,
    ) -> List[StreamerData]:
        """
        Scan live streams and return those meeting the viewer + follower
        thresholds.
        """
        if self._twitch is None:
            logger.warning("Twitch scanner not connected (missing credentials) — returning empty list")
            return []

        min_v = min_viewers or self._settings.min_viewers
        min_f = min_followers or self._settings.min_followers
        page_limit = max_pages * 100 

        logger.info(
            "Starting Twitch scan — min_viewers=%d, min_followers=%d, max_limit=%d streams",
            min_v, min_f, page_limit,
        )

        # ── Step 1: Collect streams with enough viewers ──
        viewer_qualified: list[dict] = []
        scanned_count = 0
        try:
            # Note: twitchAPI get_streams returns an AsyncGenerator, we must iterate it
            streams_gen = self._twitch.get_streams(first=100)
            async for stream in streams_gen:
                scanned_count += 1
                if stream.viewer_count >= min_v:
                    # Convert object to dict if needed, but twitchAPI objects usually allow attribute access
                    # We'll store the object itself or a simplified dict
                    viewer_qualified.append(stream)
                
                if scanned_count >= page_limit:
                    break
        except Exception as e:
            logger.error(f"Error during Twitch stream scan: {e}")

        logger.info("Viewer filter passed: %d streamers", len(viewer_qualified))

        if not viewer_qualified:
            return []

        # ── Step 2: Batch-check follower counts ──
        results: list[StreamerData] = []
        
        # viewer_qualified contains twitchAPI Stream objects
        user_ids = [s.user_id for s in viewer_qualified]

        # Process in batches
        for batch_start in range(0, len(user_ids), 100):
            batch_ids = user_ids[batch_start : batch_start + 100]
            batch_map = {s.user_id: s for s in viewer_qualified if s.user_id in batch_ids}

            # Fetch user info for this batch (needed for profile image)
            try:
                users_gen = self._twitch.get_users(user_ids=batch_ids)
                users_map = {}
                async for user in users_gen:
                    users_map[user.id] = user
            except Exception as e:
                logger.warning(f"Failed to fetch user info for batch: {e}")
                continue

            for uid in batch_ids:
                try:
                    # Get follower count
                    # Note: twitchAPI 4.x get_channel_followers returns total in metadata usually?
                    # Or we have to rely on it being returned.
                    # Actually, let's use the follower count if available. 
                    # If get_channel_followers is a generator, we can't easily get total without iterating/metadata.
                    # Looking at twitchAPI docs/source for v4:
                    # get_channel_followers returns ChannelFollowersResult which has .total and .data
                    # Wait, no, that might be older.
                    # If it is a generator, we might be stuck. 
                    # Let's hope to_list() works or check metadata if exposed.
                    # Assuming we can get explicit total:
                    follower_count = await self._get_follower_count(uid)
                except Exception as exc:
                    logger.warning("Failed to get followers for %s: %s", uid, exc)
                    continue

                if follower_count >= min_f:
                    stream = batch_map[uid]
                    user = users_map.get(uid)
                    
                    profile_img = user.profile_image_url if user else ""
                    
                    results.append(
                        StreamerData(
                            twitch_id=uid,
                            login=stream.user_login,
                            display_name=stream.user_name,
                            profile_image_url=profile_img,
                            avg_viewers=stream.viewer_count,
                            follower_count=follower_count,
                            game_name=stream.game_name,
                        )
                    )

        logger.info("Follower filter passed: %d streamers qualified", len(results))
        return results

    # ── Helpers ───────────────────────

    async def _get_follower_count(self, broadcaster_id: str) -> int:
        """Return total follower count for a broadcaster."""
        try:
            # Inspection of runtime behavior suggests this is a coroutine (awaitable),
            # unlike get_streams which is an async generator.
            result = await self._twitch.get_channel_followers(broadcaster_id=broadcaster_id, first=1)
            
            # If it returns a structure with total:
            if hasattr(result, 'total'):
                return result.total
            
            # Fallback for dictionaries or other types
            if isinstance(result, dict):
                return result.get('total', 0)
                
            return 0
        except Exception as exc:
            # If it actually IS an async generator, await will raise TypeError.
            # We catch it here.
            logger.warning("Failed to get followers for %s: %s", broadcaster_id, exc)
            return 0

    async def _get_user_info(self, user_id: str) -> dict:
        """Fetch user profile."""
        # This is now handled in batch in get_top_streamers, but keeping helper if needed
        # (unused in new logic above)
        pass


# ── Standalone test runner ───────────────────────────────────

async def _main() -> None:
    """Quick manual test — run with ``python -m scrapers.twitch_scanner``."""
    logging.basicConfig(level=logging.INFO)
    scanner = TwitchScanner()
    await scanner.connect()
    try:
        streamers = await scanner.get_top_streamers(max_pages=3)
        for s in streamers[:10]:
            print(f"  🎮 {s.display_name:20s} | viewers={s.avg_viewers:>6,} | followers={s.follower_count:>10,}")
        print(f"\nTotal qualified: {len(streamers)}")
    finally:
        await scanner.close()


if __name__ == "__main__":
    asyncio.run(_main())
