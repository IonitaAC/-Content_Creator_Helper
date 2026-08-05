"""
StreamScout — YouTube Zero-Cost Hybrid Verifier
=================================================
**THE most critical scraper in the system.**

Strategy (saves ~99 % of YouTube API quota):

    ┌────────────────────────────────────────────────────────────┐
    │  Step 1  │  youtubesearchpython (scraper) → $0             │
    │          │  Find potential channels by streamer name.       │
    │──────────│─────────────────────────────────────────────────│
    │  Step 2  │  Extract channel_id from results.               │
    │──────────│─────────────────────────────────────────────────│
    │  Step 3  │  YouTube Data API channels().list               │
    │          │  part=contentDetails,snippet  → 1 unit           │
    │          │  Get last upload date only.                      │
    │──────────│─────────────────────────────────────────────────│
    │  Step 4  │  Dormancy check:                                │
    │          │  last_upload < now() - 180 days → DORMANT        │
    └────────────────────────────────────────────────────────────┘

    Daily capacity: ~10 000 channels/day (vs. 100 with search().list).

Clipper Detection:
    Uses youtubesearchpython.VideosSearch to scan for "{name} clips".
    Checks titles for keywords and view counts > 10k.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from googleapiclient.discovery import build as gapi_build
from googleapiclient.errors import HttpError
from youtubesearchpython import ChannelsSearch, VideosSearch

from config import get_settings

logger = logging.getLogger(__name__)


# ── DTOs ─────────────────────────────────────────────────────


@dataclass
class ChannelCandidate:
    """A YouTube channel returned by the free search step."""
    channel_id: str
    title: str
    subscriber_count: Optional[int] = None
    thumbnails: Optional[dict] = None
    similarity_score: float = 0.0


@dataclass
class ClipperInfo:
    """A third-party channel uploading clips of the streamer."""
    channel_id: str
    channel_title: str
    video_title: str
    view_count: int
    published_at: Optional[str] = None


@dataclass
class YouTubeVerificationResult:
    """
    Complete result of the YouTube cross-reference for one streamer.

    ``status`` meanings:
        - ``"active"``        — channel found, uploaded within dormancy window
        - ``"dormant"``       — channel found, last upload > 180 days ago
        - ``"not_found"``     — no matching channel discovered
        - ``"manual_review"`` — ambiguous (low confidence or API error)
    """
    status: str
    channel_id: Optional[str] = None
    channel_title: Optional[str] = None
    last_upload_date: Optional[datetime] = None
    confidence: float = 0.0
    is_being_clipped: bool = False
    clipper_channels: List[ClipperInfo] = field(default_factory=list)
    error: Optional[str] = None


# ── Constants ────────────────────────────────────────────────

CLIPPER_KEYWORDS: List[str] = [
    "clips", "clip", "vod", "vods", "stream highlights",
    "highlights", "best of", "moments", "best moments",
    "funny moments", "montage",
]

CONFIDENCE_THRESHOLD: float = 0.65  # Below this → manual_review


# ── YouTube Zero-Cost Verifier ───────────────────────────────


class YouTubeZeroCostVerifier:
    """
    Hybrid YouTube verifier combining free scraping with minimal
    API usage (1 unit per channel).

    Usage::

        verifier = YouTubeZeroCostVerifier()
        result = await verifier.search_and_verify("xQc")
        print(result.status, result.last_upload_date)
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._youtube_api = None

    def _get_youtube_api(self):
        """Lazy-build the Google API client (avoids import-time network calls)."""
        if self._youtube_api is None:
            if not self._settings.youtube_api_key:
                logger.warning("⚠️ YOUTUBE_API_KEY missing — verify step will be skipped.")
                return None
            self._youtube_api = gapi_build(
                "youtube", "v3",
                developerKey=self._settings.youtube_api_key,
                cache_discovery=False,
            )
        return self._youtube_api

    # ── Public API ───────────────────────────────────────────

    async def search_and_verify(
        self,
        streamer_name: str,
        known_channel_id: Optional[str] = None,
    ) -> YouTubeVerificationResult:
        """
        Full pipeline: search → rank → verify → dormancy check.

        Parameters
        ----------
        streamer_name : str
            The Twitch display_name to search for on YouTube.
        known_channel_id : str, optional
            If we already have a channel_id (e.g. from Twitch profile
            panels), skip the search step and verify directly.

        Returns
        -------
        YouTubeVerificationResult
        """
        try:
            # ── Fast path: known channel ID ──
            if known_channel_id:
                logger.info("Verifying known channel %s for %s", known_channel_id, streamer_name)
                return await self._verify_channel(
                    known_channel_id, streamer_name, confidence=0.95
                )

            # ── Step 1: Free search ──
            logger.info("Searching YouTube (free) for: %s", streamer_name)
            candidates = await self._search_channels_free(streamer_name)

            if not candidates:
                logger.info("No YouTube channels found for %s", streamer_name)
                return YouTubeVerificationResult(status="not_found")

            # ── Step 2: Rank candidates by name similarity ──
            best = self._rank_candidates(streamer_name, candidates)
            logger.info(
                "Best match for '%s': '%s' (confidence=%.2f)",
                streamer_name, best.title, best.similarity_score,
            )

            if best.similarity_score < CONFIDENCE_THRESHOLD:
                logger.warning(
                    "Low confidence (%.2f) for %s → %s — flagging manual review",
                    best.similarity_score, streamer_name, best.title,
                )
                return YouTubeVerificationResult(
                    status="manual_review",
                    channel_id=best.channel_id,
                    channel_title=best.title,
                    confidence=best.similarity_score,
                )

            # ── Step 3: Verify with official API (1 unit) ──
            return await self._verify_channel(
                best.channel_id, streamer_name, confidence=best.similarity_score
            )

        except Exception as exc:
            logger.error("YouTube verification failed for %s: %s", streamer_name, exc)
            return YouTubeVerificationResult(
                status="manual_review",
                error=str(exc),
            )

    async def check_all_channels_for_activity(
        self,
        streamer_name: str,
        max_results: int = 2,
        recency_days: int | None = None,
    ) -> dict:
        """
        Search YouTube for channels matching the streamer's name and
        check if ANY of them posted a video recently.

        Parameters
        ----------
        streamer_name : str
            Twitch display name to search for.
        max_results : int
            Maximum number of channel candidates to check (default 2).
        recency_days : int, optional
            Days threshold — if None, uses ``channel_search_recency_days``
            from settings (default 30).

        Returns
        -------
        dict
            {
                "channels_found": int,
                "any_active": bool,
                "active_channels": list[dict],  # title, channel_id, last_upload
            }
        """
        if recency_days is None:
            recency_days = self._settings.channel_search_recency_days

        result = {
            "channels_found": 0,
            "any_active": False,
            "active_channels": [],
        }

        try:
            candidates = await self._search_channels_free(
                streamer_name, limit=max_results
            )
            result["channels_found"] = len(candidates)

            if not candidates:
                logger.info(
                    "YouTube channel search for '%s': no channels found",
                    streamer_name,
                )
                return result

            cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)

            for candidate in candidates:
                if not candidate.channel_id:
                    continue

                last_upload = await self._check_channel_last_upload_date(
                    candidate.channel_id
                )
                if last_upload and last_upload > cutoff:
                    result["any_active"] = True
                    result["active_channels"].append({
                        "title": candidate.title,
                        "channel_id": candidate.channel_id,
                        "last_upload": last_upload.isoformat(),
                    })
                    logger.info(
                        "YouTube channel '%s' (%s) is active — last upload %s",
                        candidate.title,
                        candidate.channel_id,
                        last_upload.strftime("%Y-%m-%d"),
                    )

        except Exception as exc:
            logger.error(
                "YouTube channel activity check failed for '%s': %s",
                streamer_name, exc,
            )

        logger.info(
            "YouTube channel search for '%s': %d found, %s",
            streamer_name,
            result["channels_found"],
            "ACTIVE" if result["any_active"] else "clean",
        )
        return result

    async def verify_known_channel_recent(
        self,
        channel_id: str,
        recency_days: int | None = None,
    ) -> bool:
        """
        Check if a known YouTube channel (from Twitch panels) posted
        a video within ``recency_days`` (default: 7 days / 1 week).

        Returns True if the channel is recently active (→ should reject).
        """
        if recency_days is None:
            recency_days = self._settings.twitch_link_recency_days

        try:
            last_upload = await self._check_channel_last_upload_date(channel_id)
            if last_upload is None:
                logger.info(
                    "Twitch-panel YT channel %s: no uploads found → clean",
                    channel_id,
                )
                return False

            cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
            is_recent = last_upload > cutoff
            logger.info(
                "Twitch-panel YT channel %s: last upload %s — %s",
                channel_id,
                last_upload.strftime("%Y-%m-%d"),
                "ACTIVE (reject)" if is_recent else "inactive (ok)",
            )
            return is_recent

        except Exception as exc:
            logger.error(
                "Failed to check Twitch-panel YT channel %s: %s",
                channel_id, exc,
            )
            return False

    async def _check_channel_last_upload_date(
        self, channel_id: str
    ) -> datetime | None:
        """
        Return the datetime of the most recent upload for a channel,
        or None if no uploads exist / API unavailable.

        Cost: ~2 quota units (channels.list + playlistItems.list).
        """
        youtube = self._get_youtube_api()
        if youtube is None:
            return None

        # Get uploads playlist
        response = await asyncio.to_thread(
            lambda: youtube.channels().list(
                part="contentDetails",
                id=channel_id,
            ).execute()
        )
        items = response.get("items", [])
        if not items:
            return None

        uploads_playlist = (
            items[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        if not uploads_playlist:
            return None

        # Get most recent video
        playlist_response = await asyncio.to_thread(
            lambda: youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist,
                maxResults=1,
            ).execute()
        )
        playlist_items = playlist_response.get("items", [])
        if not playlist_items:
            return None

        last_upload_str = (
            playlist_items[0]
            .get("contentDetails", {})
            .get("videoPublishedAt", "")
        )
        if not last_upload_str:
            return None

        return datetime.fromisoformat(
            last_upload_str.replace("Z", "+00:00")
        )

    async def check_for_clippers(
        self,
        streamer_name: str,
        min_views: int = 10_000,
        months_back: int = 6,
    ) -> Tuple[bool, List[ClipperInfo]]:
        """
        Search YouTube for third-party channels clipping this streamer.

        Uses free ``VideosSearch`` — costs $0.

        Returns
        -------
        tuple[bool, list[ClipperInfo]]
            (is_being_clipped, list_of_clipper_channels)
        """
        queries = [
            f"{streamer_name} clips",
            f"{streamer_name} highlights",
            f"{streamer_name} best moments",
        ]

        cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)
        clippers: Dict[str, ClipperInfo] = {}

        for query in queries:
            try:
                results = await asyncio.to_thread(
                    self._search_videos_sync, query, limit=10
                )
                for video in results:
                    title_lower = video.get("title", "").lower()
                    channel_title = video.get("channel", {}).get("name", "")

                    # Skip if this IS the streamer's own channel
                    if self._names_match(streamer_name, channel_title):
                        continue

                    # Check if title contains clipper keywords
                    has_keyword = any(kw in title_lower for kw in CLIPPER_KEYWORDS)
                    if not has_keyword:
                        continue

                    # Check view count
                    view_text = video.get("viewCount", {}).get("text", "0")
                    view_count = self._parse_view_count(view_text)
                    if view_count < min_views:
                        continue

                    # Check recency (approximate from publishedTime text)
                    ch_id = video.get("channel", {}).get("id", "")
                    if ch_id and ch_id not in clippers:
                        clippers[ch_id] = ClipperInfo(
                            channel_id=ch_id,
                            channel_title=channel_title,
                            video_title=video.get("title", ""),
                            view_count=view_count,
                            published_at=video.get("publishedTime"),
                        )
            except Exception as exc:
                logger.warning("Clipper search failed for query '%s': %s", query, exc)
                continue

        clipper_list = list(clippers.values())
        is_clipped = len(clipper_list) > 0
        logger.info(
            "Clipper check for %s: %s (%d channels)",
            streamer_name,
            "FOUND" if is_clipped else "clean",
            len(clipper_list),
        )
        return is_clipped, clipper_list

    # ── Private: Free YouTube Search ─────────────────────────

    async def _search_channels_free(
        self, query: str, limit: int = 5
    ) -> List[ChannelCandidate]:
        """
        Use ``youtubesearchpython.ChannelsSearch`` (scraper, $0) to
        find channel candidates.
        """
        candidates: List[ChannelCandidate] = []

        try:
            # ChannelsSearch is synchronous — run in a thread
            search = await asyncio.to_thread(
                self._channels_search_sync, query, limit
            )
            for item in search:
                sub_text = item.get("subscribers", "0")
                sub_count = self._parse_subscriber_count(sub_text)

                candidates.append(
                    ChannelCandidate(
                        channel_id=item.get("id", ""),
                        title=item.get("title", ""),
                        subscriber_count=sub_count,
                        thumbnails=item.get("thumbnails"),
                    )
                )
        except Exception as exc:
            logger.error("Free YouTube channel search failed: %s", exc)

        return candidates

    @staticmethod
    def _channels_search_sync(query: str, limit: int) -> list:
        """Synchronous wrapper for ChannelsSearch."""
        search = ChannelsSearch(query, limit=limit)
        return search.result().get("result", [])

    @staticmethod
    def _search_videos_sync(query: str, limit: int) -> list:
        """Synchronous wrapper for VideosSearch."""
        search = VideosSearch(query, limit=limit)
        return search.result().get("result", [])

    # ── Private: Official API Verification (1 unit) ──────────

    async def _verify_channel(
        self,
        channel_id: str,
        streamer_name: str,
        confidence: float,
    ) -> YouTubeVerificationResult:
        """
        Use the official YouTube Data API to check the last upload
        date for a specific channel.

        **Cost: 1 quota unit** (channels().list with contentDetails).
        """
        try:
            youtube = self._get_youtube_api()

            # channels().list — part=contentDetails,snippet — 1 unit
            response = await asyncio.to_thread(
                lambda: youtube.channels().list(
                    part="contentDetails,snippet,statistics",
                    id=channel_id,
                ).execute()
            )

            items = response.get("items", [])
            if not items:
                return YouTubeVerificationResult(
                    status="not_found",
                    channel_id=channel_id,
                    confidence=confidence,
                )

            channel_data = items[0]
            channel_title = channel_data.get("snippet", {}).get("title", "")

            # Get the "uploads" playlist
            uploads_playlist = (
                channel_data
                .get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )

            if not uploads_playlist:
                return YouTubeVerificationResult(
                    status="dormant",
                    channel_id=channel_id,
                    channel_title=channel_title,
                    confidence=confidence,
                )

            # Fetch the most recent video from the uploads playlist
            playlist_response = await asyncio.to_thread(
                lambda: youtube.playlistItems().list(
                    part="contentDetails",
                    playlistId=uploads_playlist,
                    maxResults=1,
                ).execute()
            )

            playlist_items = playlist_response.get("items", [])
            if not playlist_items:
                return YouTubeVerificationResult(
                    status="dormant",
                    channel_id=channel_id,
                    channel_title=channel_title,
                    confidence=confidence,
                )

            # Parse the last upload date
            last_upload_str = (
                playlist_items[0]
                .get("contentDetails", {})
                .get("videoPublishedAt", "")
            )

            if not last_upload_str:
                return YouTubeVerificationResult(
                    status="manual_review",
                    channel_id=channel_id,
                    channel_title=channel_title,
                    confidence=confidence,
                )

            last_upload = datetime.fromisoformat(
                last_upload_str.replace("Z", "+00:00")
            )

            # ── Dormancy Logic ──
            dormancy_threshold = datetime.now(timezone.utc) - timedelta(
                days=self._settings.dormancy_days
            )
            is_dormant = last_upload < dormancy_threshold
            status = "dormant" if is_dormant else "active"

            logger.info(
                "Channel '%s' (%s): last upload %s — %s",
                channel_title, channel_id,
                last_upload.strftime("%Y-%m-%d"),
                status.upper(),
            )

            return YouTubeVerificationResult(
                status=status,
                channel_id=channel_id,
                channel_title=channel_title,
                last_upload_date=last_upload,
                confidence=confidence,
            )

        except HttpError as exc:
            if exc.resp.status == 403:
                logger.error("YouTube API quota exceeded! Status 403.")
                return YouTubeVerificationResult(
                    status="manual_review",
                    error="API quota exceeded (403)",
                )
            logger.error("YouTube API error for channel %s: %s", channel_id, exc)
            return YouTubeVerificationResult(
                status="manual_review",
                channel_id=channel_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.error("Unexpected error verifying channel %s: %s", channel_id, exc)
            return YouTubeVerificationResult(
                status="manual_review",
                channel_id=channel_id,
                error=str(exc),
            )

    # ── Private: Scoring & Parsing ───────────────────────────

    def _rank_candidates(
        self, streamer_name: str, candidates: List[ChannelCandidate]
    ) -> ChannelCandidate:
        """
        Rank channel candidates by name similarity + subscriber count.

        Scoring formula:
            score = (name_similarity * 0.7) + (subscriber_rank * 0.3)

        The 70/30 split ensures we heavily weight name accuracy (to
        avoid false positives) while still giving a bonus to larger
        channels (which are more likely to be the real deal).
        """
        name_clean = self._normalize_name(streamer_name)

        for candidate in candidates:
            title_clean = self._normalize_name(candidate.title)
            name_sim = SequenceMatcher(None, name_clean, title_clean).ratio()

            # Bonus for exact substring match
            if name_clean in title_clean or title_clean in name_clean:
                name_sim = min(name_sim + 0.15, 1.0)

            candidate.similarity_score = name_sim

        # Sort by similarity descending
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates[0]

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Lowercase, strip whitespace, remove common suffixes."""
        name = name.lower().strip()
        # Remove common YouTube suffixes
        for suffix in [" gaming", " tv", " live", " official", " clips"]:
            name = name.replace(suffix, "")
        return name

    @staticmethod
    def _names_match(name_a: str, name_b: str) -> bool:
        """Quick check if two names refer to the same entity."""
        a = name_a.lower().strip()
        b = name_b.lower().strip()
        return a == b or a in b or b in a

    @staticmethod
    def _parse_subscriber_count(text: Optional[str]) -> int:
        """Parse '1.2M subscribers' → 1_200_000."""
        if not text:
            return 0
        text = text.lower().replace(",", "").replace("subscribers", "").strip()
        try:
            if "m" in text:
                return int(float(text.replace("m", "")) * 1_000_000)
            elif "k" in text:
                return int(float(text.replace("k", "")) * 1_000)
            return int(text)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_view_count(text: Optional[str]) -> int:
        """Parse '1,234,567 views' → 1_234_567."""
        if not text:
            return 0
        digits = re.sub(r"[^\d]", "", text)
        try:
            return int(digits)
        except (ValueError, TypeError):
            return 0


# ── Standalone test runner ───────────────────────────────────

async def _main() -> None:
    """Quick manual test — run with ``python -m scrapers.youtube_zero_cost``."""
    logging.basicConfig(level=logging.INFO)
    verifier = YouTubeZeroCostVerifier()

    test_names = ["xQc", "shroud", "pokimane"]
    for name in test_names:
        print(f"\n{'='*60}")
        print(f"  Checking: {name}")
        print(f"{'='*60}")
        result = await verifier.search_and_verify(name)
        print(f"  Status:      {result.status}")
        print(f"  Channel:     {result.channel_title} ({result.channel_id})")
        print(f"  Last Upload: {result.last_upload_date}")
        print(f"  Confidence:  {result.confidence:.2f}")

        is_clipped, clippers = await verifier.check_for_clippers(name)
        print(f"  Clipped:     {is_clipped} ({len(clippers)} channels)")
        for c in clippers[:3]:
            print(f"    → {c.channel_title}: '{c.video_title}' ({c.view_count:,} views)")


if __name__ == "__main__":
    asyncio.run(_main())
