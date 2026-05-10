import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    Enum as SAEnum, JSON, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ProcessingStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    failed = "failed"


class TopicStatus(str, enum.Enum):
    active = "active"
    resolved = "resolved"
    merged_into = "merged_into"


class NewsletterSource(Base):
    __tablename__ = "newsletter_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender_email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    trust_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    newsletters: Mapped[List["Newsletter"]] = relationship(back_populates="source")


class Newsletter(Base):
    __tablename__ = "newsletters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("newsletter_sources.id"), nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False, default="")
    date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    body_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    source: Mapped["NewsletterSource"] = relationship(back_populates="newsletters")
    newsletter_articles: Mapped[List["NewsletterArticle"]] = relationship(back_populates="newsletter")


class CanonicalStory(Base):
    """A deduplicated cluster of articles covering the same story."""
    __tablename__ = "canonical_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # representative_article_id must be a plain column (no FK constraint) to break the
    # circular reference with articles.canonical_story_id. The FK is enforced at app level.
    representative_article_id: Mapped[int] = mapped_column(Integer, nullable=False)
    article_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    articles: Mapped[List["Article"]] = relationship(
        back_populates="canonical_story",
        foreign_keys="[Article.canonical_story_id]",
    )
    summaries: Mapped[List["ArticleSummary"]] = relationship(back_populates="canonical_story")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_paywalled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        SAEnum(ProcessingStatus), default=ProcessingStatus.pending, nullable=False
    )
    canonical_story_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("canonical_stories.id"), nullable=True
    )

    canonical_story: Mapped[Optional["CanonicalStory"]] = relationship(
        back_populates="articles",
        foreign_keys=[canonical_story_id],
    )
    newsletter_articles: Mapped[List["NewsletterArticle"]] = relationship(back_populates="article")
    summaries: Mapped[List["ArticleSummary"]] = relationship(
        back_populates="article",
        foreign_keys="[ArticleSummary.article_id]",
    )


class NewsletterArticle(Base):
    """Many-to-many join: newsletters ↔ articles, with the blurb the newsletter wrote about it."""
    __tablename__ = "newsletter_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    newsletter_id: Mapped[int] = mapped_column(ForeignKey("newsletters.id"), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    blurb: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # surrounding text from the email

    newsletter: Mapped["Newsletter"] = relationship(back_populates="newsletter_articles")
    article: Mapped["Article"] = relationship(back_populates="newsletter_articles")


class ArticleSummary(Base):
    """LLM-generated summary for a canonical story. One per canonical_story."""
    __tablename__ = "article_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_story_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_stories.id"), nullable=False, unique=True
    )
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    significance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False, default="v1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    canonical_story: Mapped["CanonicalStory"] = relationship(back_populates="summaries")
    article: Mapped["Article"] = relationship(
        back_populates="summaries",
        foreign_keys=[article_id],
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    status: Mapped[TopicStatus] = mapped_column(
        SAEnum(TopicStatus), default=TopicStatus.active, nullable=False
    )
    merged_into_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topics.id"), nullable=True)

    digest_topics: Mapped[List["DigestTopic"]] = relationship(back_populates="topic")


class TopicArticle(Base):
    """Join: topics ↔ articles ↔ digests."""
    __tablename__ = "topic_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    digest_id: Mapped[Optional[int]] = mapped_column(ForeignKey("digests.id"), nullable=True)


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    date_range_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    date_range_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    output_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    digest_topics: Mapped[List["DigestTopic"]] = relationship(back_populates="digest")


class DigestTopic(Base):
    __tablename__ = "digest_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), nullable=False)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    significance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    what_is_new_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    digest: Mapped["Digest"] = relationship(back_populates="digest_topics")
    topic: Mapped["Topic"] = relationship(back_populates="digest_topics")


class PipelineWatermark(Base):
    """Stores last successful run timestamps for incremental fetching."""
    __tablename__ = "pipeline_watermarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
