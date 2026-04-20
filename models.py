from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


# ── Base ─────────────────────────────────────────────────────


class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


# ── Enums ────────────────────────────────────────────────────


class YouTubeStatus(str, enum.Enum):
    """Result of the YouTube cross-reference check."""
    ACTIVE = "active"                # Has a channel AND uploaded within 6 months
    DORMANT = "dormant"              # Has a channel BUT no upload in >6 months
    NOT_FOUND = "not_found"          # No matching channel discovered
    MANUAL_REVIEW = "manual_review"  # Ambiguous — needs human eyes


class LeadStatus(str, enum.Enum):
    """CRM pipeline stage for a streamer lead."""
    NEW_LEAD = "new_lead"
    CONTACTED = "contacted"
    IN_NEGOTIATION = "in_negotiation"
    SIGNED = "signed"
    REJECTED = "rejected"


class Platform(str, enum.Enum):
    """Social platform source for GigHunt posts."""
    TWITTER = "twitter"
    REDDIT = "reddit"


# ── Streamer ─────────────────────────────────────────────────


class Streamer(Base):
    """
    A Twitch streamer discovered by the scanner.

    This is the **central entity**.  Every downstream check (YouTube,
    clippers, leads) hangs off this record via foreign keys.
    """
    __tablename__ = "streamers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Twitch identity ──
    twitch_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    login: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # ── Twitch metrics (snapshot at scan time) ──
    avg_viewers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    follower_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    game_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # ── YouTube cross-ref result ──
    youtube_status: Mapped[YouTubeStatus] = mapped_column(
        Enum(YouTubeStatus, name="youtube_status_enum"),
        nullable=False,
        default=YouTubeStatus.MANUAL_REVIEW,
    )
    has_clippers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Timestamps ──
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ── Relationships ──
    youtube_channels: Mapped[List["YouTubeChannel"]] = relationship(
        "YouTubeChannel", back_populates="streamer", cascade="all, delete-orphan"
    )
    lead: Mapped[Optional["Lead"]] = relationship(
        "Lead", back_populates="streamer", uselist=False, cascade="all, delete-orphan"
    )

    # ── Indexes ──
    __table_args__ = (
        Index("ix_streamers_yt_status", "youtube_status"),
        Index("ix_streamers_viewers_followers", "avg_viewers", "follower_count"),
    )

    def __repr__(self) -> str:
        return (
            f"<Streamer {self.display_name} "
            f"viewers={self.avg_viewers} followers={self.follower_count} "
            f"yt={self.youtube_status.value}>"
        )


# ── YouTube Channel ──────────────────────────────────────────


class YouTubeChannel(Base):
    """
    A YouTube channel linked (or potentially linked) to a Streamer.

    Multiple rows may exist per streamer when the search returns
    several candidates — only the one marked ``is_official=True``
    is the confirmed match.
    """
    __tablename__ = "youtube_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── YouTube identity ──
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    subscriber_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_upload_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Verification ──
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_clipper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ── Foreign key ──
    streamer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("streamers.id", ondelete="CASCADE"), nullable=False
    )
    streamer: Mapped["Streamer"] = relationship("Streamer", back_populates="youtube_channels")

    # ── Timestamps ──
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<YouTubeChannel {self.title} "
            f"official={self.is_official} clipper={self.is_clipper} "
            f"last_upload={self.last_upload_date}>"
        )


# ── Social Post (GigHunt) ───────────────────────────────────


class SocialPost(Base):
    """
    A hiring / gig post discovered on Twitter or Reddit.

    Deduplicated by ``(platform, post_id)`` — Redis guards against
    re-inserting the same tweet/post within a scan window.
    """
    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Source ──
    platform: Mapped[Platform] = mapped_column(
        Enum(Platform, name="platform_enum"), nullable=False
    )
    post_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[str] = mapped_column(String(256), nullable=False)
    author_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # ── Content ──
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    query_matched: Mapped[str] = mapped_column(String(256), nullable=False)

    # ── Engagement snapshot ──
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Timestamps ──
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Constraints ──
    __table_args__ = (
        Index("ix_social_platform_post", "platform", "post_id", unique=True),
        Index("ix_social_posted_at", "posted_at"),
    )

    def __repr__(self) -> str:
        return f"<SocialPost {self.platform.value}:{self.post_id} by @{self.author}>"


# ── Lead (CRM) ──────────────────────────────────────────────


class Lead(Base):
    """
    CRM record tracking a streamer through the sales pipeline.

    One-to-one with :class:`Streamer`.
    """
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Link ──
    streamer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("streamers.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    streamer: Mapped["Streamer"] = relationship("Streamer", back_populates="lead")

    # ── Pipeline ──
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status_enum"),
        nullable=False,
        default=LeadStatus.NEW_LEAD,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Revenue proxy ──
    estimated_monthly_revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Lead streamer_id={self.streamer_id} status={self.status.value}>"
