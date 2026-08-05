"""
StreamScout — Clipper Checker Service
=======================================
Isolated logic for detecting third-party YouTube channels that are
uploading clips, VODs, or highlights of a given Twitch streamer.

This is called by the cross-reference pipeline after the primary
YouTube verification step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Tuple

from scrapers.youtube_zero_cost import ClipperInfo, YouTubeZeroCostVerifier

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────

CLIPPER_KEYWORDS: List[str] = [
    "clips", "clip", "vod", "vods", "stream highlights",
    "highlights", "best of", "moments", "best moments",
    "funny moments", "montage",
]

MIN_VIEW_COUNT: int = 10_000      # Ignore low-view clipper videos
MONTHS_LOOKBACK: int = 1          # Only check last month's clips (YouTube only)


# ── Service ──────────────────────────────────────────────────


class ClipperChecker:
    """
    Checks whether a streamer is already being monetised by
    third-party clip channels on YouTube.

    Usage::

        checker = ClipperChecker()
        is_clipped, clippers = await checker.check("xQc")
    """

    def __init__(self) -> None:
        self._verifier = YouTubeZeroCostVerifier()

    async def check(
        self,
        streamer_name: str,
        min_views: int = MIN_VIEW_COUNT,
        months_back: int = MONTHS_LOOKBACK,
    ) -> Tuple[bool, List[ClipperInfo]]:
        """
        Run the clipper detection pipeline.

        Parameters
        ----------
        streamer_name : str
            Twitch display name.
        min_views : int
            Minimum view count to consider a clip significant.
        months_back : int
            How far back to look for clips.

        Returns
        -------
        tuple[bool, list[ClipperInfo]]
            (is_being_clipped, list_of_clipper_channels)
        """
        logger.info("Running clipper check for '%s'", streamer_name)

        is_clipped, clippers = await self._verifier.check_for_clippers(
            streamer_name=streamer_name,
            min_views=min_views,
            months_back=months_back,
        )

        if is_clipped:
            logger.info(
                "⚠️  '%s' is being clipped by %d channel(s):",
                streamer_name, len(clippers),
            )
            for c in clippers:
                logger.info(
                    "   → %s: '%s' (%s views)",
                    c.channel_title, c.video_title, f"{c.view_count:,}",
                )
        else:
            logger.info("✅ '%s' has no significant clipper channels", streamer_name)

        return is_clipped, clippers
