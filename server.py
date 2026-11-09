# -*- coding: utf-8 -*-
"""Expose a DeepSeek-backed AgentScope agent as an A2A 1.0 server.

Adapted from AgentScope v2.0.11 examples/a2a/server.py (Apache-2.0).

An ``AgentExecutor`` bridges the two event models: the agent's streamed
``TextBlockDeltaEvent`` chunks are published as artifact chunks, so an A2A
client sees the reply arrive incrementally. Each A2A ``context_id`` gets its
own agent, which is what keeps the conversations independent. Run with::

    export DEEPSEEK_API_KEY=sk-...
    python server.py [--model deepseek-flash] [--port 9999]
"""
import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
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
from starlette.applications import Starlette
from starlette.routing import Route

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential
from agentscope.event import TextBlockDeltaEvent
from agentscope.message import UserMsg
from agentscope.model import DeepSeekChatModel

from traces import DEFAULT_TRACE_DB, TraceRun, TraceStore


class AgentScopeExecutor(AgentExecutor):
    """Run one AgentScope agent per A2A context."""

    def __init__(self, model: str, api_key: str, traces: TraceStore) -> None:
        """Remember how to build an agent for a new conversation."""
        self._model = model
        self._api_key = api_key
        self._traces = traces
        self._agents: dict[str, Agent] = {}

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Stream the agent's reply back as A2A artifact chunks."""
        if context.task_id is None or context.context_id is None:
            raise RuntimeError("A2A server did not assign Task/context IDs.")

        user_input = context.get_user_input()
        with self._traces.run(context.context_id, context.task_id, user_input) as trace:
            await self._reply(context, event_queue, user_input, trace)

    async def _reply(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        user_input: str,
        trace: TraceRun,
    ) -> None:
        if context.context_id not in self._agents:
            self._agents[context.context_id] = Agent(
                name="Friday",
                system_prompt="You are a helpful assistant named Friday.",
                model=DeepSeekChatModel(
                    credential=DeepSeekCredential(api_key=self._api_key),
                    model=self._model,
                    stream=True,
                ),
            )
        agent = self._agents[context.context_id]

        # The Task itself must reach the client before any update to it.
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            ),
        )
        updater = TaskUpdater(
            event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        await updater.start_work()
        trace.record("A2A_WORKING", {})

        # One artifact per reply, streamed chunk by chunk. Each chunk is
        # held back one step so the last one can be marked as such.
        artifact_id = f"{context.task_id}-reply"
        pending, started = None, False
        async for event in agent.reply_stream(
            UserMsg(name="user", content=user_input),
        ):
            trace.record_event(event)
            if not isinstance(event, TextBlockDeltaEvent):
                continue
            if pending is not None:
                await updater.add_artifact(
                    [Part(text=pending)],
                    artifact_id=artifact_id,
                    append=started,
                    last_chunk=False,
                )
                started = True
            pending = event.delta

        if pending is not None:
            await updater.add_artifact(
                [Part(text=pending)],
                artifact_id=artifact_id,
                append=started,
                last_chunk=True,
            )
        await updater.complete()

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Mark the Task canceled at the client's request."""
        if context.task_id is None or context.context_id is None:
            raise RuntimeError("Cannot cancel a Task without IDs.")
        await TaskUpdater(
            event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        ).cancel()


def create_app(
    base_url: str, model: str, api_key: str, trace_db: Path = DEFAULT_TRACE_DB,
) -> Starlette:
    """Build the A2A 1.0 application serving the agent."""
    card = AgentCard(
        name="Friday",
        description="An AgentScope assistant exposed over A2A 1.0.",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            ),
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="chat",
                name="Chat",
                description="Answer questions in a multi-turn conversation.",
                tags=["chat"],
            ),
        ],
    )
    traces = TraceStore(trace_db)
    return create_a2a_app(card, AgentScopeExecutor(model, api_key, traces), traces)


def create_a2a_app(card: AgentCard, executor: AgentExecutor, traces: TraceStore) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    if urlsplit(card.supported_interfaces[0].url).hostname in {"127.0.0.1", "localhost", "::1"}:
        routes.append(Route("/traces", traces.view, methods=["GET"]))
    return Starlette(routes=routes)


async def main() -> None:
    """The main entry point of the server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--trace-db", type=Path, default=DEFAULT_TRACE_DB)
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set the DEEPSEEK_API_KEY environment variable before "
            "running this demo.",
        )

    app = create_app(
        base_url=f"http://{args.host}:{args.port}",
        model=args.model,
        api_key=api_key,
        trace_db=args.trace_db,
    )
    await uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port),
    ).serve()


if __name__ == "__main__":
    asyncio.run(main())
