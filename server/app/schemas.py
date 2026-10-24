from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- endpoints & models -------------------------------------------------

EndpointKind = Literal["openai_compatible", "anthropic", "agent_cli", "mock"]


class EndpointIn(BaseModel):
    name: str
    kind: EndpointKind
    driver: Literal["claude_code", "cursor", "copilot"] | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    auto_models: bool | list[str] | None = None  # default: True for OpenRouter


class EndpointPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # "" clears the key
    enabled: bool | None = None
    auto_models: bool | list[str] | None = None


class EndpointOut(ORM):
    id: int
    name: str
    kind: str
    driver: str | None
    base_url: str | None
    api_key_masked: str | None = None
    source: str
    enabled: bool
    auto_models: bool | list[str] = False
    status: str = "unchecked"
    status_detail: str = ""
    checked_at: datetime | None = None


class ModelIn(BaseModel):
    endpoint_id: int
    model_name: str
    display_name: str | None = None
    color: str = "#6b7280"
    input_price: float = 0.0
    output_price: float = 0.0
    enabled: bool = True
    supports_tools: bool = True


class ModelPatch(BaseModel):
    display_name: str | None = None
    color: str | None = None
    input_price: float | None = None
    output_price: float | None = None
    enabled: bool | None = None
    supports_tools: bool | None = None


class ModelOut(ORM):
    id: int
    endpoint_id: int
    endpoint_name: str = ""
    endpoint_kind: str = ""
    model_name: str
    display_name: str
    color: str
    input_price: float
    output_price: float
    enabled: bool
    supports_tools: bool
    supports_images: bool = True
    source: str
    available: bool = True
    availability_detail: str = ""


# ---- ssh ------------------------------------------------------------------


class SshIn(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    auth: Literal["key", "password", "agent"] = "key"
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None
    shell: Literal["bash", "powershell"] = "bash"


class SshPatch(BaseModel):
    host: str | None = None
    port: int | None = None
    username: str | None = None
    auth: Literal["key", "password", "agent"] | None = None
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None
    shell: Literal["bash", "powershell"] | None = None


class SshOut(ORM):
    id: int
    name: str
    host: str
    port: int
    username: str
    auth: str
    shell: str
    host_key_fingerprint: str | None = None
    source: str


class HostKeyTrust(BaseModel):
    fingerprint: str


# ---- runners ---------------------------------------------------------------


class RunnerOut(ORM):
    id: int
    name: str
    os: str
    shell: str
    hostname: str
    roots: list[str]
    clis: dict[str, Any]
    version: str
    last_seen: datetime | None
    online: bool = False


# ---- sessions ----------------------------------------------------------------


class WorkspaceIn(BaseModel):
    kind: Literal["none", "runner", "ssh"] = "none"
    runner_id: int | None = None
    ssh_connection_id: int | None = None
    path: str | None = None


class SessionIn(BaseModel):
    reuse_empty: bool = False  # return (and update) the latest session instead, if it has no messages yet
    title: str = "New session"
    context_mode: Literal["shared", "isolated"] = "isolated"
    model_ids: list[int] = Field(default_factory=list)
    workspace: WorkspaceIn = Field(default_factory=WorkspaceIn)


class SessionPatch(BaseModel):
    title: str | None = None
    phase: Literal["plan", "build"] | None = None
    context_mode: Literal["shared", "isolated"] | None = None
    builder_model_id: int | None = None
    approve_edits: Literal["auto", "ask"] | None = None
    approve_commands: Literal["auto", "ask"] | None = None


class ParticipantOut(BaseModel):
    model_id: int
    display_name: str
    color: str
    endpoint_kind: str
    position: int


class SessionOut(ORM):
    id: int
    title: str
    workspace_kind: str
    runner_id: int | None
    ssh_connection_id: int | None
    workspace_path: str | None
    workspace_label: str = ""
    phase: str
    context_mode: str
    builder_model_id: int | None
    approve_edits: str
    approve_commands: str
    created_at: datetime
    updated_at: datetime
    participants: list[ParticipantOut] = Field(default_factory=list)
    busy: bool = False
    cost: float = 0.0  # USD spent in this session, all models
    tokens: int = 0


class SessionModelsIn(BaseModel):
    model_ids: list[int]


class MessageOut(ORM):
    id: int
    session_id: int
    seq: int
    turn_id: str
    role: str
    model_id: int | None
    phase: str
    content: str
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    recipients: list[int] | None
    attachments: list[dict[str, Any]] | None = None
    status: str
    created_at: datetime
    vote: int = 0
    usage: dict[str, Any] | None = None  # {input_tokens, output_tokens, cost, estimated} for this reply


class ChatIn(BaseModel):
    content: str
    recipients: list[int] | None = None  # model ids; None = all participants (PLAN) / builder (BUILD)
    attachment_ids: list[int] = Field(default_factory=list)  # from POST /api/sessions/{id}/attachments


class AttachmentIn(BaseModel):
    filename: str
    mime: str | None = None
    data_base64: str


class AttachmentOut(BaseModel):
    id: int
    filename: str
    mime: str
    size: int
    kind: str  # image | document | binary
    text_chars: int | None = None


class ApprovalIn(BaseModel):
    approve: bool
    note: str | None = None


class PickBuilderIn(BaseModel):
    model_id: int
    switch_to_build: bool = True


class VoteIn(BaseModel):
    value: Literal[-1, 0, 1]


class RevertIn(BaseModel):
    turn_id: str | None = None  # default: latest turn with changes


class UsageRow(BaseModel):
    model_id: int | None
    display_name: str
    color: str
    input_tokens: int
    output_tokens: int
    cost: float
    estimated: bool


class FileChangeOut(ORM):
    id: int
    turn_id: str
    model_id: int | None
    path: str
    before: str | None
    after: str | None
    reverted: bool
    created_at: datetime
