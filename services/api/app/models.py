"""Core PostgreSQL data model for Super Signals."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user_links: Mapped[list[UserRole]] = relationship(back_populates="role")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'revoked')",
            name="ck_users_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="invited")
    pin_hash: Mapped[str | None] = mapped_column(String(255))
    two_factor_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    passkey_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    role_links: Mapped[list[UserRole]] = relationship(
        foreign_keys="UserRole.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    telegram_accounts: Mapped[list[TelegramAccount]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(
        foreign_keys=[user_id],
        back_populates="role_links",
    )
    role: Mapped[Role] = relationship(back_populates="user_links")


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_invitations_single_terminal_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TelegramAccount(TimestampMixin, Base):
    __tablename__ = "telegram_accounts"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "phone_number_e164",
            name="uq_telegram_accounts_owner_phone",
        ),
        CheckConstraint(
            "status IN ('pending', 'connected', 'disconnected', 'revoked')",
            name="ck_telegram_accounts_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    phone_number_e164: Mapped[str] = mapped_column(String(24), nullable=False)
    session_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    session_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(back_populates="telegram_accounts")
    sources: Mapped[list[Source]] = relationship(
        back_populates="telegram_account", cascade="all, delete-orphan"
    )


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint(
            "telegram_account_id",
            "chat_id",
            name="uq_sources_account_chat",
        ),
        CheckConstraint(
            "status IN ('testing', 'paused', 'live', 'revoked')",
            name="ck_sources_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    telegram_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(255))
    source_alias: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="testing")
    redistribution_permission_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    permission_notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    telegram_account: Mapped[TelegramAccount] = relationship(back_populates="sources")
    messages: Mapped[list[Message]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "telegram_message_id",
            name="uq_messages_source_telegram_id",
        ),
        CheckConstraint(
            "ingestion_status IN ('received', 'parsed', 'skipped', 'duplicate', 'error')",
            name="ck_messages_ingestion_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingestion_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="received"
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[Source] = relationship(back_populates="messages")
    signal: Mapped[Signal | None] = relationship(back_populates="source_message", uselist=False)


class Signal(TimestampMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_signals_side"),
        CheckConstraint(
            "order_type IN ('market', 'pending')",
            name="ck_signals_order_type",
        ),
        CheckConstraint(
            "parser_status IN ('accepted', 'skipped', 'review')",
            name="ck_signals_parser_status",
        ),
        CheckConstraint("risk_multiplier > 0", name="ck_signals_risk_multiplier"),
        CheckConstraint(
            "entry_high IS NULL OR entry_low IS NULL OR entry_high >= entry_low",
            name="ck_signals_entry_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    symbol: Mapped[str | None] = mapped_column(String(40))
    side: Mapped[str | None] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(12), nullable=False, server_default="market")
    entry_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    entry_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    parser_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="review")
    skip_reason: Mapped[str | None] = mapped_column(Text)
    risk_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, server_default="1"
    )
    original_text: Mapped[str] = mapped_column(Text, nullable=False)

    source_message: Mapped[Message] = relationship(back_populates="signal")
    positions: Mapped[list[Position]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "signal_id",
            "user_id",
            "tp_index",
            name="uq_positions_signal_user_tp",
        ),
        CheckConstraint("tp_index > 0", name="ck_positions_tp_index"),
        CheckConstraint(
            "planned_risk_percent IN (0.5, 1.0, 1.5, 2.0, 3.0, 4.0)",
            name="ck_positions_risk_percent",
        ),
        CheckConstraint(
            "status IN ('planned', 'open', 'closed', 'skipped', 'error')",
            name="ck_positions_status",
        ),
        Index(
            "uq_positions_broker_position_id",
            "broker_position_id",
            unique=True,
            postgresql_where=text("broker_position_id IS NOT NULL"),
        ),
        Index(
            "uq_positions_broker_client_id",
            "broker_client_id",
            unique=True,
            postgresql_where=text("broker_client_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    signal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("signals.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    tp_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    planned_risk_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    broker_order_id: Mapped[str | None] = mapped_column(String(120))
    broker_position_id: Mapped[str | None] = mapped_column(String(120))
    broker_client_id: Mapped[str | None] = mapped_column(String(31))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="planned")
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    pnl_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    close_reason: Mapped[str | None] = mapped_column(String(80))

    signal: Mapped[Signal] = relationship(back_populates="positions")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
