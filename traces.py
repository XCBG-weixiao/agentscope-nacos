"""Persist and display agent events for each A2A execution."""

import asyncio
import json
import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from agentscope.event import AgentEvent, EventType
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates


DEFAULT_TRACE_DB = Path.home() / ".local/state/agentscope-demo/traces.sqlite3"
TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent)
TEMPLATES.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False}

TEXT_DELTAS = {
    EventType.TEXT_BLOCK_DELTA: "block_id",
    EventType.TOOL_CALL_DELTA: "tool_call_id",
    EventType.TOOL_RESULT_TEXT_DELTA: "tool_call_id",
}
TEXT_ENDS = {
    EventType.TEXT_BLOCK_END: (EventType.TEXT_BLOCK_DELTA, "text"),
    EventType.TOOL_CALL_END: (EventType.TOOL_CALL_DELTA, "arguments"),
    EventType.TOOL_RESULT_END: (EventType.TOOL_RESULT_TEXT_DELTA, "output"),
}
TERMINAL_EVENTS = {
    "A2A_COMPLETED": "已完成",
    "A2A_FAILED": "失败",
    "A2A_CANCELED": "已取消",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class TraceStore:
    def __init__(self, path: Path, agent_name: str = "Friday") -> None:
        self.path = path.expanduser()
        self.agent_name = agent_name
        self.active_runs: set[str] = set()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trace_events_session
                    ON trace_events (session_id, id);
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def run(self, session_id: str, task_id: str, user_input: str):
        run = TraceRun(self, session_id, task_id)
        run.record("A2A_REQUEST", {"text": user_input})
        self.active_runs.add(run.run_id)
        try:
            yield run
        except asyncio.CancelledError:
            run.record("A2A_CANCELED", {})
            raise
        except Exception as exc:
            run.record("A2A_FAILED", {"error_type": type(exc).__name__})
            raise
        else:
            run.record("A2A_COMPLETED", {})
        finally:
            self.active_runs.remove(run.run_id)

    async def view(self, request: Request) -> Response:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            return Response(status_code=403)

        with closing(self.connect()) as db:
            sessions = db.execute("""
                SELECT session_id, COUNT(DISTINCT run_id) AS runs
                FROM trace_events GROUP BY session_id ORDER BY MAX(id) DESC
            """).fetchall()
            session_id = request.query_params.get("session_id", "").strip()
            if not session_id and sessions:
                session_id = sessions[0]["session_id"]
            events = db.execute("""
                SELECT task_id, run_id, kind, created_at, payload
                FROM trace_events WHERE session_id = ? ORDER BY id
            """, (session_id,)).fetchall()

        runs = {}
        for event in events:
            run = runs.setdefault(event["run_id"], {
                "task_id": event["task_id"],
                "started_at": event["created_at"],
                "status": "进行中" if event["run_id"] in self.active_runs else "已中断",
                "events": [],
            })
            if event["kind"] in TERMINAL_EVENTS:
                run["status"] = TERMINAL_EVENTS[event["kind"]]
            run["events"].append({**dict(event), "details": json.loads(event["payload"])})

        return TEMPLATES.TemplateResponse(
            request, "trace_view.html", {
                "agent_name": self.agent_name,
                "session_id": session_id,
                "sessions": [
                    {**dict(session), "url": "/traces?" + urlencode({"session_id": session["session_id"]})}
                    for session in sessions
                ],
                "runs": list(reversed(runs.values())),
            }, headers={"Cache-Control": "no-store"},
        )


class TraceRun:
    def __init__(self, store: TraceStore, session_id: str, task_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.task_id = task_id
        self.run_id = str(uuid4())
        self.deltas: dict[tuple[str, str], list[str]] = defaultdict(list)

    def record(self, kind: str, payload: dict, timestamp: str | None = None) -> None:
        with closing(self.store.connect()) as db, db:
            db.execute("""
                INSERT INTO trace_events (session_id, task_id, run_id, kind, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self.session_id, self.task_id, self.run_id, kind,
                  timestamp or now(), json.dumps(payload, ensure_ascii=False)))

    def record_event(self, event: AgentEvent) -> None:
        kind = event.type
        if kind in TEXT_DELTAS:
            self.deltas[kind, getattr(event, TEXT_DELTAS[kind])].append(event.delta)
            return
        if kind.endswith("_DELTA"):
            return

        details = event.model_dump(mode="json", exclude={"id", "type", "created_at", "metadata"},
                                   exclude_none=True)
        if kind in TEXT_ENDS:
            delta_kind, field = TEXT_ENDS[kind]
            chunks = self.deltas.pop((delta_kind, getattr(event, TEXT_DELTAS[delta_kind])), [])
            if chunks:
                details.setdefault(field, "".join(chunks))
        self.record(kind, details, event.created_at)
