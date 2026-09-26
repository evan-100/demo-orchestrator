import multiprocessing as mp
from datetime import UTC, datetime

from orchestrator.core.ledger import EventType, Ledger, LedgerEvent


def _ev(i: int, actor="operator") -> LedgerEvent:
    return LedgerEvent(
        ts=datetime(2026, 9, 24, tzinfo=UTC),
        event=EventType.REQUESTED,
        env=f"e{i}",
        namespace=f"demo-e{i}",
        actor=actor,
        details={"pad": "x" * 2000},
    )


def _writer(path, actor, n):
    led = Ledger(path)
    for i in range(n):
        led.append(_ev(i, actor))


def test_roundtrip(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    led.append(_ev(1))
    assert [e.env for e in led.read()] == ["e1"]


def test_concurrent_writers_never_interleave(tmp_path):
    p = tmp_path / "l.jsonl"
    procs = [mp.Process(target=_writer, args=(p, a, 200)) for a in ("operator", "sweeper")]
    [x.start() for x in procs]
    [x.join() for x in procs]
    events = list(Ledger(p).read())
    assert len(events) == 400
    assert len(p.read_text().splitlines()) == 400


def test_malformed_line_skipped(tmp_path, caplog):
    p = tmp_path / "l.jsonl"
    led = Ledger(p)
    led.append(_ev(1))
    with p.open("a") as f:
        f.write('{"ts": "2026-09-24T00:00:00Z", "event": "rea\n')  # truncated write
    led.append(_ev(2))
    assert [e.env for e in led.read()] == ["e1", "e2"]
    assert "malformed" in caplog.text
