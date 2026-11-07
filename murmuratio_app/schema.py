"""Normalized values exchanged between source adapters and calculations.

The public API retains its established flat representation; these types describe
that representation before it is serialized, not the vendor's on-disk formats.
"""
from dataclasses import dataclass
from typing import Generic, Literal, TypedDict, TypeVar

Confidence = Literal['observed', 'inferred']
SourceStatus = Literal['available', 'unavailable', 'unsupported', 'stale']
SessionStatus = Literal['working', 'hung', 'waiting', 'idle', 'done', 'blocked', 'crashed']


class Parent(TypedDict, total=False):
    parent_id: str | None
    parent_session: str
    parent_app_session: str
    orchestrator: bool


class Action(TypedDict, total=False):
    last_action: str
    current_action: str


class SessionTimes(TypedDict, total=False):
    started: float | None
    ended: float | None
    last_at: float | None
    turn_started: float | None
    waiting_since: float | None
    seconds: float | None


class Context(TypedDict, total=False):
    context_tokens: int | None
    context_used: int | None
    context_window: int | None


class Session(Parent, Action, SessionTimes, Context, total=False):
    id: str
    kind: str
    model: str
    who: str
    task: str
    status: SessionStatus
    project: str
    cwd: str
    folder: str
    completed: bool
    app_session: str | None
    thread: str | None


class DesktopSession(TypedDict, total=False):
    cli_session_id: str
    app_session: str
    title: str | None
    model: str | None
    cwd: str | None
    archived: bool | None
    parent_session: str
    has_parent: bool
    scheduled_task_id: str | None


class LimitWindow(TypedDict):
    window_minutes: float
    used_percent: float
    resets_at: float | None


class Usage(TypedDict):
    at: float | None
    message_id: str | None
    model: str
    input_tokens: int | float
    cache_creation_input_tokens: int | float
    cache_read_input_tokens: int | float
    output_tokens: int | float


class QuotaHistory(TypedDict):
    at: float
    tool: str
    window: str
    used_percent: float
    resets_at: float | None


class ClaudeUsageBuckets(TypedDict):
    kind: Literal['worker', 'chat']
    hours: dict[int, float]
    models: dict[int, dict[str, float]]


CreditReading = tuple[float, float]
CodexBurnReading = tuple[float, float, float | None, str]


class CodexMeter(TypedDict):
    points: list[CreditReading]
    usage: list[CodexBurnReading]
    latest: CreditReading | None


class ContextTimeline(TypedDict):
    points: list[dict]
    compactions: list[dict]
    last: dict
    last_at: str | float | None
    last_limit: int | None
    pending_compact: bool
    model: str | None
    effort: str | None
    top: list[dict]
    hashes: dict[str, int]
    categories: dict[str, int]
    quota: dict | None
    malformed: int


T = TypeVar('T')


@dataclass(frozen=True)
class Observation(Generic[T]):
    source: str
    timestamp: float | None
    confidence: Confidence
    value: T


@dataclass(frozen=True)
class AdapterResult(Generic[T]):
    source: str
    status: SourceStatus
    observations: list[Observation[T]]
    age_seconds: float | None = None

    def values(self) -> list[T]:
        return [observation.value for observation in self.observations]
