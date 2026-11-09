"""Serve the open-source Codex CLI as a local A2A 1.0 agent."""

import argparse
import asyncio
import json
import os
import shutil
import signal
from contextlib import closing, suppress
from pathlib import Path
from weakref import WeakValueDictionary

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import InvalidParamsError

from server import create_a2a_app
from traces import TraceRun, TraceStore


DEFAULT_TRACE_DB = Path.home() / ".local/state/codex-a2a/traces.sqlite3"
CODEX_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL",
    "CODEX_HOME", "CODEX_API_KEY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


class CodexExecutor(AgentExecutor):
    def __init__(
        self, binary: str, workspace: Path, sandbox: str, model: str | None,
        timeout: int, traces: TraceStore,
    ) -> None:
        self.command = [
            binary, "exec", "--json", "--ignore-user-config", "--sandbox", sandbox,
            "-c", 'web_search="disabled"',
            "-c", "allow_login_shell=false",
            "-c", 'shell_environment_policy.inherit="none"',
        ]
        if model:
            self.command.extend(["--model", model])
        self.env = {key: os.environ[key] for key in CODEX_ENV_KEYS if key in os.environ}
        self.workspace = workspace
        self.timeout = timeout
        self.traces = traces
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        with closing(traces.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS codex_sessions (
                    context_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL
                )
            """)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        if task_id is None or context_id is None:
            raise RuntimeError("A2A server did not assign Task/context IDs.")
        message = context.message
        prompt = context.get_user_input()
        if message is None or not prompt.strip() or any(not part.HasField("text") for part in message.parts):
            raise InvalidParamsError(message="Send one or more nonempty text parts to Codex.")

        await event_queue.enqueue_event(Task(
            id=task_id, context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        ))
        updater = TaskUpdater(event_queue, task_id=task_id, context_id=context_id)
        lock = self.locks.get(context_id)
        if lock is None:
            lock = self.locks[context_id] = asyncio.Lock()
        try:
            with self.traces.run(context_id, task_id, prompt) as trace:
                async with lock:
                    await updater.start_work()
                    trace.record("A2A_WORKING", {})
                    reply = await self.run_codex(context_id, prompt, trace)
                    await updater.add_artifact([Part(text=reply)], name="Codex reply")
                    await updater.complete()
        except (OSError, RuntimeError, ValueError) as exc:
            await updater.failed(updater.new_agent_message([Part(text=str(exc))]))

    async def run_codex(self, context_id: str, prompt: str, trace: TraceRun) -> str:
        with closing(self.traces.connect()) as db:
            previous = db.execute(
                "SELECT thread_id FROM codex_sessions WHERE context_id = ?", (context_id,),
            ).fetchone()
        command = self.command.copy()
        if previous:
            command.extend(["resume", previous["thread_id"]])
        command.append("-")  # The A2A caller's text is read from stdin.

        thread_id, reply, error = None, None, None
        completed = False
        try:
            async with asyncio.timeout(self.timeout):
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=self.workspace, env=self.env, start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
                )
                try:
                    process.stdin.write(prompt.encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    async for line in process.stdout:
                        event = json.loads(line)
                        kind = event["type"]
                        if kind.startswith("item.") and event["item"]["type"] == "reasoning":
                            continue
                        trace.record("CODEX_" + kind.replace(".", "_").upper(), {
                            key: value for key, value in event.items() if key != "type"
                        })
                        if kind == "thread.started":
                            thread_id = event["thread_id"]
                        elif kind == "item.completed" and event["item"]["type"] == "agent_message":
                            reply = event["item"]["text"]
                        elif kind == "turn.completed":
                            completed = True
                        elif kind in {"error", "turn.failed"}:
                            error = event.get("message") if kind == "error" else event["error"]["message"]
                    exit_code = await process.wait()
                finally:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await process.communicate()
        except TimeoutError as exc:
            raise RuntimeError(f"Codex exceeded the {self.timeout}s execution limit.") from exc

        if exit_code != 0 or not completed or not thread_id or not reply:
            raise RuntimeError(error or f"Codex did not complete with a reply (exit code {exit_code}).")
        if previous and thread_id != previous["thread_id"]:
            raise RuntimeError("Codex returned a different thread ID when resuming.")
        if not previous:
            with closing(self.traces.connect()) as db, db:
                db.execute("INSERT INTO codex_sessions VALUES (?, ?)", (context_id, thread_id))
        return reply

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The A2A handler cancels execute() after this event, which stops the CLI process.
        await TaskUpdater(event_queue, task_id=context.task_id, context_id=context.context_id).cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write"], default="read-only")
    parser.add_argument("--model", help="Override the Codex CLI's default model")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per Codex turn")
    parser.add_argument("--trace-db", type=Path, default=DEFAULT_TRACE_DB)
    args = parser.parse_args()
    binary = shutil.which(args.codex)
    if not binary:
        parser.error(f"Codex executable not found: {args.codex}")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace does not exist: {workspace}")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    card = AgentCard(
        name="Codex",
        description="OpenAI Codex CLI coding agent exposed over A2A 1.0.",
        version="1.0.0",
        supported_interfaces=[AgentInterface(
            url=f"http://127.0.0.1:{args.port}", protocol_binding="JSONRPC", protocol_version="1.0",
        )],
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(
            id="coding", name="Coding", description="Work on code in a configured Git workspace.",
            tags=["code"],
        )],
    )
    traces = TraceStore(args.trace_db, agent_name=card.name)
    executor = CodexExecutor(binary, workspace, args.sandbox, args.model, args.timeout, traces)
    uvicorn.run(create_a2a_app(card, executor, traces), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
