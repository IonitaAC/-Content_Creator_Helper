"""
StreamScout — Cross-Reference Pipeline
========================================
Orchestrates the full **Twitch → YouTube** verification flow:

    1. Scan Twitch for top-tier streamers (>1k viewers, >100k followers).
    2. For each streamer, extract YouTube link from Twitch profile (if any).
    3. Verify channel via the hybrid YouTube model.
    4. Run clipper detection.
    5. Upsert results into the database.

This is the **brain** of StreamScout — the pipeline that creates
actionable leads from raw Twitch data.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Lead, LeadStatus, Streamer, YouTubeChannel, YouTubeStatus
from scrapers.twitch_scanner import StreamerData, TwitchScanner
from scrapers.youtube_zero_cost import YouTubeVerificationResult, YouTubeZeroCostVerifier
from services.clipper_checker import ClipperChecker

logger = logging.getLogger(__name__)


# ── Revenue Estimation ───────────────────────────────────────

# Conservative RPM (revenue per 1000 views) estimates for gaming content
RPM_ESTIMATE_USD: float = 3.50
# Assume YouTube views ≈ 10% of average Twitch viewers × 30 days × 3 videos/week
VIEW_MULTIPLIER: float = 0.1 * 30 * 3


def estimate_monthly_revenue(avg_viewers: int) -> float:
    """
    Rough estimate of monthly YouTube revenue based on Twitch metrics.

    Formula:
        estimated_views = avg_viewers × 0.10 × 30 days × 3 vids/week
        revenue = estimated_views × RPM / 1000
    """
    estimated_views = avg_viewers * VIEW_MULTIPLIER
    return round(estimated_views * RPM_ESTIMATE_USD / 1000, 2)


# ── YouTube Link Extraction ─────────────────────────────────

YOUTUBE_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/(?:channel/|c/|@)?([a-zA-Z0-9_\-]+)",
    re.IGNORECASE,
)


def extract_youtube_channel_id_from_url(url: str) -> Optional[str]:
    """
    Attempt to extract a channel identifier from a YouTube URL.

    Handles:
        - youtube.com/channel/UC...
        - youtube.com/c/ChannelName
        - youtube.com/@handle
        - youtu.be/... (not a channel, but we catch it)

    Returns the identifier part (not necessarily a channel_id — may
    need further resolution via YouTube API).
    """
    match = YOUTUBE_URL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


# ── Cross-Reference Pipeline ────────────────────────────────


class CrossReferencePipeline:
    """
    Full Twitch → YouTube → Lead pipeline.

    Usage::

        pipeline = CrossReferencePipeline(db_session)
        results = await pipeline.run()
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._settings = get_settings()
        self._twitch = TwitchScanner()
        self._youtube = YouTubeZeroCostVerifier()
        self._clipper = ClipperChecker()

    async def run(
        self,
        max_pages: int = 20,
        skip_existing: bool = True,
    ) -> List[dict]:
        """
        Execute the full pipeline.

        Parameters
        ----------
        max_pages : int
            Twitch stream pages to scan (100 streams per page).
        skip_existing : bool
            Skip streamers already in the DB (avoid re-scanning).

        Returns
        -------
        list[dict]
            Summary dicts for each processed streamer.
        """
        results: List[dict] = []

        # ── Step 1: Twitch Scan ──
        logger.info("═══ PIPELINE START ═══")
        await self._twitch.connect()
        try:
            streamers = await self._twitch.get_top_streamers(max_pages=max_pages)
        finally:
            await self._twitch.close()

        logger.info("Twitch scan returned %d qualified streamers", len(streamers))

        # ── Step 2: Process each streamer ──
        for i, streamer_data in enumerate(streamers, 1):
            logger.info(
                "[%d/%d] Processing: %s",
                i, len(streamers), streamer_data.display_name,
            )

            try:
                result = await self._process_one(streamer_data, skip_existing)
                results.append(result)
            except Exception as exc:
                logger.error(
                    "Failed to process %s: %s",
                    streamer_data.display_name, exc,
                )
                results.append({
                    "streamer": streamer_data.display_name,
                    "status": "error",
                    "error": str(exc),
                })

        logger.info("═══ PIPELINE COMPLETE — processed %d streamers ═══", len(results))
        return results

    async def _process_one(
        self,
        data: StreamerData,
        skip_existing: bool,
    ) -> dict:
        """Process a single streamer through the 3-tier verification pipeline.

        Tier 1: Twitch-panel YouTube link → reject if last video < 1 week
        Tier 2: YouTube channel name search → reject if any channel posted < 1 month
        Tier 3: YouTube highlight/clip search → reject if clips found < 1 month
        """

        # ── Check if already exists ──
        if skip_existing:
            existing = await self._db.execute(
                select(Streamer).where(Streamer.twitch_id == data.twitch_id)
            )
            if existing.scalar_one_or_none():
                logger.debug("Skipping %s — already in DB", data.display_name)
                return {"streamer": data.display_name, "status": "skipped"}

        yt_status = YouTubeStatus.NOT_FOUND
        is_clipped = False
        rejection_reason = None
        yt_result = None

        # ════════════════════════════════════════════════════════
        # TIER 1: Twitch-panel YouTube link check (1-week window)
        # ════════════════════════════════════════════════════════
        known_channel_id = None
        if data.youtube_url_from_panels:
            known_channel_id = extract_youtube_channel_id_from_url(
                data.youtube_url_from_panels
            )

        if known_channel_id:
            logger.info(
                "  [Tier 1] Checking Twitch-panel YT link for %s: %s",
                data.display_name, known_channel_id,
            )
            is_recently_active = await self._youtube.verify_known_channel_recent(
                known_channel_id
            )
            if is_recently_active:
                yt_status = YouTubeStatus.ACTIVE
                rejection_reason = "Twitch-panel YT channel active (posted within 1 week)"
                logger.info(
                    "  ❌ REJECTED %s — %s", data.display_name, rejection_reason
                )
        else:
            logger.info(
                "  [Tier 1] No YouTube link in Twitch panels for %s — clean",
                data.display_name,
            )

        # ════════════════════════════════════════════════════════
        # TIER 2: YouTube channel name search (1-month window)
        # ════════════════════════════════════════════════════════
        if yt_status != YouTubeStatus.ACTIVE:
            logger.info(
                "  [Tier 2] Searching YouTube for channels named '%s'…",
                data.display_name,
            )
            channel_activity = await self._youtube.check_all_channels_for_activity(
                data.display_name
            )
            if channel_activity["any_active"]:
                yt_status = YouTubeStatus.ACTIVE
                active_names = [
                    ch["title"] for ch in channel_activity["active_channels"]
                ]
                rejection_reason = (
                    f"YouTube channel(s) active in last month: {', '.join(active_names)}"
                )
                logger.info(
                    "  ❌ REJECTED %s — %s", data.display_name, rejection_reason
                )
            else:
                logger.info(
                    "  [Tier 2] %s: %d channels found, none active — clean",
                    data.display_name, channel_activity["channels_found"],
                )

        # ════════════════════════════════════════════════════════
        # TIER 3: YouTube highlight/clip check (1-month window)
        # ════════════════════════════════════════════════════════
        if yt_status != YouTubeStatus.ACTIVE:
            logger.info(
                "  [Tier 3] Checking YouTube for highlights/clips of %s…",
                data.display_name,
            )
            is_clipped, clippers = await self._clipper.check(data.display_name)
            if is_clipped:
                rejection_reason = (
                    f"YouTube highlight/clip channels found ({len(clippers)} channels)"
                )
                logger.info(
                    "  ❌ REJECTED %s — %s", data.display_name, rejection_reason
                )
            else:
                logger.info(
                    "  [Tier 3] %s: no YouTube highlights/clips — clean ✅",
                    data.display_name,
                )
        else:
            # Skip clipper check — already rejected
            clippers = []

        # ── Upsert Streamer ──
        streamer = Streamer(
            twitch_id=data.twitch_id,
            login=data.login,
            display_name=data.display_name,
            profile_image_url=data.profile_image_url,
            avg_viewers=data.avg_viewers,
            follower_count=data.follower_count,
            game_name=data.game_name,
            youtube_status=yt_status,
            has_clippers=is_clipped,
        )
        self._db.add(streamer)
        await self._db.flush()  # Get streamer.id

        # ── Add clipper channels if any ──
        for clipper in clippers:
            clipper_channel = YouTubeChannel(
                channel_id=clipper.channel_id,
                title=clipper.channel_title,
                is_official=False,
                is_clipper=True,
                confidence_score=0.0,
                streamer_id=streamer.id,
            )
            self._db.add(clipper_channel)

        # ── Create Lead ONLY if all 3 tiers passed ──
        lead_created = False
        if yt_status == YouTubeStatus.NOT_FOUND and not is_clipped:
            lead = Lead(
                streamer_id=streamer.id,
                status=LeadStatus.NEW_LEAD,
                estimated_monthly_revenue=estimate_monthly_revenue(data.avg_viewers),
            )
            self._db.add(lead)
            lead_created = True
            logger.info(
                "  ✅ LEAD CREATED for %s (viewers=%d, followers=%d)",
                data.display_name, data.avg_viewers, data.follower_count,
            )

        await self._db.commit()

        summary = {
            "streamer": data.display_name,
            "twitch_id": data.twitch_id,
            "twitch_link": f"https://twitch.tv/{data.login}",
            "viewers": data.avg_viewers,
            "followers": data.follower_count,
            "youtube_status": yt_status.value,
            "has_clippers": is_clipped,
            "clipper_count": len(clippers),
            "lead_created": lead_created,
            "rejection_reason": rejection_reason,
        }
        logger.info("  Result: %s", summary)
        return summary

