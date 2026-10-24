from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # openai_compatible | anthropic | agent_cli | mock
    kind: Mapped[str] = mapped_column(String(32))
    # agent_cli only: claude_code | cursor
    driver: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(500))
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    # config (from config.yaml, read-only through the API) | api
    source: Mapped[str] = mapped_column(String(16), default="api")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Import the vendor's model catalog automatically: false, true (all chat models) or a list of glob patterns.
    auto_models: Mapped[Any] = mapped_column(JsonType, default=False)
    # Result of the last key/model check: unchecked | ok | no_key | error
    status: Mapped[str] = mapped_column(String(16), default="unchecked")
    status_detail: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("endpoint_id", "model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(16), default="#6b7280")
    input_price: Mapped[float] = mapped_column(Float, default=0.0)  # USD per 1M tokens
    output_price: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_images: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(16), default="api")  # config | api | catalog
    # Set by the endpoint check: can this model be used right now, and if not, why.
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    availability_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()


class SshConnection(Base):
    __tablename__ = "ssh_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(100))
    auth: Mapped[str] = mapped_column(String(16), default="key")  # key | password | agent
    private_key_enc: Mapped[str | None] = mapped_column(Text)
    passphrase_enc: Mapped[str | None] = mapped_column(Text)
    password_enc: Mapped[str | None] = mapped_column(Text)
    host_key: Mapped[str | None] = mapped_column(Text)  # pinned OpenSSH public key line
    shell: Mapped[str] = mapped_column(String(16), default="bash")  # bash | powershell
    source: Mapped[str] = mapped_column(String(16), default="api")
    created_at: Mapped[datetime] = _now()


class Runner(Base):
    __tablename__ = "runners"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    os: Mapped[str] = mapped_column(String(32), default="")
    shell: Mapped[str] = mapped_column(String(32), default="")
    hostname: Mapped[str] = mapped_column(String(255), default="")
    roots: Mapped[list[str]] = mapped_column(JsonType, default=list)
    clis: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    version: Mapped[str] = mapped_column(String(32), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="New session")
    workspace_kind: Mapped[str] = mapped_column(String(16), default="none")  # none | runner | ssh
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"))
    ssh_connection_id: Mapped[int | None] = mapped_column(ForeignKey("ssh_connections.id", ondelete="SET NULL"))
    workspace_path: Mapped[str | None] = mapped_column(String(1000))
    phase: Mapped[str] = mapped_column(String(8), default="plan")  # plan | build
    context_mode: Mapped[str] = mapped_column(String(16), default="isolated")  # shared | isolated
    builder_model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"))
    approve_edits: Mapped[str] = mapped_column(String(8), default="auto")  # auto | ask
    approve_commands: Mapped[str] = mapped_column(String(8), default="ask")
    next_seq: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SessionModel(Base):
    __tablename__ = "session_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = _now()
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # per-session display index (#n)
    turn_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"))
    phase: Mapped[str] = mapped_column(String(8))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType)
    tool_call_id: Mapped[str | None] = mapped_column(String(100))
    recipients: Mapped[list[int] | None] = mapped_column(JsonType)
    # [{id, filename, mime, size, kind}] for user messages with files
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(16), default="complete")  # streaming | complete | error | cancelled
    created_at: Mapped[datetime] = _now()


class Attachment(Base):
    """A file the user attached to a message (stored in the database so history keeps it)."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # image | document | binary
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    text: Mapped[str | None] = mapped_column(Text, deferred=True)  # extracted text for documents
    created_at: Mapped[datetime] = _now()


class Usage(Base):
    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class Vote(Base):
    __tablename__ = "votes"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), unique=True)
    value: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = _now()


class BuilderPick(Base):
    __tablename__ = "builder_picks"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = _now()


class AgentCliState(Base):
    __tablename__ = "agent_cli_state"
    __table_args__ = (UniqueConstraint("session_id", "model_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    cli_session_id: Mapped[str | None] = mapped_column(String(200))
    last_seen_message_id: Mapped[int] = mapped_column(Integer, default=0)
    last_total_cost: Mapped[float] = mapped_column(Float, default=0.0)  # CLIs report cumulative cost on resume


class FileChange(Base):
    """Journal of file writes per turn: enough to show diffs and revert a turn."""

    __tablename__ = "file_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    turn_id: Mapped[str] = mapped_column(String(36), index=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"))
    path: Mapped[str] = mapped_column(String(1000))
    before: Mapped[str | None] = mapped_column(Text)  # None = file did not exist
    after: Mapped[str | None] = mapped_column(Text)  # None = file deleted
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JsonType)
