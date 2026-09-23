"""Append-only JSONL audit ledger.

The operator and the sweeper (separate processes) both append events to the
same file; `metrics` later reads it back. Appends are made atomic across
processes with an exclusive `flock` plus a single `os.write` of the whole
line, so concurrent writers never interleave partial lines. Reads tolerate a
missing file and skip individually malformed lines rather than raising, since
a truncated write (e.g. from a killed process) must never take down readers.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_serializer, field_validator

from orchestrator.core.expiry import from_rfc3339, to_rfc3339

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    REQUESTED = "requested"
    READY = "ready"
    EXTENDED = "extended"
    EXPIRED = "expired"
    DELETED = "deleted"
    DELETE_TIMEOUT = "delete_timeout"
    FAILED = "failed"
    SWEEPER_REAPED = "sweeper_reaped"
    SWEEPER_SKIPPED = "sweeper_skipped"


class LedgerEvent(BaseModel):
    ts: datetime
    event: EventType
    env: str
    namespace: str
    persona: str | None = None
    actor: Literal["operator", "sweeper", "cli"]
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts", mode="before")
    @classmethod
    def _parse_ts(cls, value: object) -> object:
        if isinstance(value, str):
            return from_rfc3339(value)
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError(f"ts must be timezone-aware, got naive {value!r}")
        return value

    @field_serializer("ts")
    def _serialize_ts(self, value: datetime) -> str:
        return to_rfc3339(value)


class Ledger:
    """An append-only JSONL file of `LedgerEvent`s, safe for concurrent writers."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: LedgerEvent) -> None:
        """Append `event` as one JSON line, atomically with respect to other writers."""
        data = (event.model_dump_json() + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read(self) -> Iterator[LedgerEvent]:
        """Yield every well-formed event in file order.

        A missing ledger yields nothing. Blank lines are skipped silently.
        Any line that isn't valid JSON or doesn't validate as a `LedgerEvent`
        is skipped with a `logging.warning`, never raised.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed ledger line %d in %s", lineno, self._path)
                continue
            try:
                yield LedgerEvent.model_validate(raw)
            except ValidationError:
                logger.warning("skipping malformed ledger line %d in %s", lineno, self._path)
                continue
