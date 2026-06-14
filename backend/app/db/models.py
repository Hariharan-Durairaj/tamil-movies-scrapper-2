"""SQLAlchemy models. Schema is created with create_all() on startup;
idempotent ALTERs for future changes go in session.run_migrations()."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Movie pipeline statuses ──────────────────────────────────────────────
class MovieStatus:
    DISCOVERED = "discovered"            # found on forum, not yet matched
    MATCHED = "matched"                  # metadata matched with confidence
    NEEDS_REVIEW = "needs_review"        # ambiguous match — user must pick
    UNMATCHED = "unmatched"              # no metadata candidate found
    QUALIFIED = "qualified"              # rating >= threshold, ready to send
    REJECTED = "rejected"                # below threshold / other reason
    SENT = "sent"                        # delivered to radarr+qbittorrent
    FAILED = "failed"                    # send failed (see rejection_reason)
    LIBRARY = "library"                  # cataloged by full forum scan only
    IN_RADARR = "in_radarr"              # already in Radarr (sync / dedupe)

    ALL = [DISCOVERED, MATCHED, NEEDS_REVIEW, UNMATCHED, QUALIFIED,
           REJECTED, SENT, FAILED, LIBRARY, IN_RADARR]


class Movie(Base):
    __tablename__ = "movies"
    __table_args__ = (
        UniqueConstraint("title", "year", name="uq_movies_title_year"),
        Index("ix_movies_status", "status"),
        Index("ix_movies_is_tamil", "is_tamil_original"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Forum identity
    title: Mapped[str] = mapped_column(String, nullable=False)   # parsed title
    year: Mapped[int | None] = mapped_column(Integer)
    forum_title: Mapped[str | None] = mapped_column(Text)        # raw topic title
    forum_url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String, default="auto_scan")  # auto_scan | manual_search | library_scan | manual

    # Pipeline state
    status: Mapped[str] = mapped_column(String, default=MovieStatus.DISCOVERED)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Metadata match
    matched_title: Mapped[str | None] = mapped_column(String)
    original_language: Mapped[str | None] = mapped_column(String)
    is_tamil_original: Mapped[bool | None] = mapped_column(Boolean)
    imdb_id: Mapped[str | None] = mapped_column(String)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)
    rating_source: Mapped[str | None] = mapped_column(String)    # tmdb | omdb | imdb
    match_confidence: Mapped[float | None] = mapped_column(Float)
    match_candidates: Mapped[list | None] = mapped_column(JSON)  # top candidates for review UI
    poster_path: Mapped[str | None] = mapped_column(String)      # local webp, served at /posters/

    # Languages advertised in the forum post (evidence, e.g. ["tamil","telugu"])
    forum_languages: Mapped[list | None] = mapped_column(JSON)

    # Delivery
    selected_torrent_id: Mapped[int | None] = mapped_column(Integer)
    downloaded_quality: Mapped[str | None] = mapped_column(String)
    added_to_radarr: Mapped[bool] = mapped_column(Boolean, default=False)
    added_to_qbittorrent: Mapped[bool] = mapped_column(Boolean, default=False)
    radarr_skip_reason: Mapped[str | None] = mapped_column(String)  # e.g. not_in_tmdb

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    torrents: Mapped[list["MovieTorrent"]] = relationship(
        back_populates="movie", cascade="all, delete-orphan")


class MovieTorrent(Base):
    """Every torrent variant found in a movie's forum post."""
    __tablename__ = "movie_torrents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(Text)
    torrent_url: Mapped[str] = mapped_column(Text)               # http .torrent or magnet:
    is_magnet: Mapped[bool] = mapped_column(Boolean, default=False)
    source_format: Mapped[str | None] = mapped_column(String)    # fileext | magnet | ipsAttachLink
    quality: Mapped[str | None] = mapped_column(String)
    codec: Mapped[str | None] = mapped_column(String)
    rip_type: Mapped[str | None] = mapped_column(String)
    file_size: Mapped[str | None] = mapped_column(String)
    languages: Mapped[list | None] = mapped_column(JSON)
    torrent_file_path: Mapped[str | None] = mapped_column(Text)  # pre-downloaded .torrent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    movie: Mapped[Movie] = relationship(back_populates="torrents")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LogEntry(Base):
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainHistory(Base):
    """Every domain the site has ever been seen on. Used for cheap
    redirect-follow rediscovery before launching Chrome."""
    __tablename__ = "domain_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String, unique=True)     # www.1tamilmv.cards
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)


class MetadataCache(Base):
    """Cache of external metadata lookups (TMDB/OMDB/IMDb) keyed by query."""
    __tablename__ = "metadata_cache"
    __table_args__ = (UniqueConstraint("source", "query_key", name="uq_metacache"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String)                  # tmdb_search | imdb_suggest | ...
    query_key: Mapped[str] = mapped_column(String)
    payload: Mapped[dict | list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskState(Base):
    """Small key/value store for job state (full-scan checkpoint, last runs)."""
    __tablename__ = "task_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
