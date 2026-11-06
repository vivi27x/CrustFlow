"""The long-lived objects one process shares."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Coroutine

from app.config import Settings
from app.crustdata.client import CrustdataClient
from app.delivery.mail import Mailer
from app.delivery.slack import SlackNotifier
from app.llm import LLM
from app.personas import VendorPersona, load_personas


@dataclass
class Services:
    settings: Settings
    client: CrustdataClient
    llm: LLM
    mailer: Mailer
    slack: SlackNotifier
    personas: dict[str, VendorPersona] = field(default_factory=dict)
    # The account balance is the only true count of what an agent run spends, and it can't tell two
    # runs apart, so only one runs at a time.
    agent_turn: asyncio.Lock = field(default_factory=asyncio.Lock)
    reports_in_progress: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)
    pill_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    watch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)  # The event loop only keeps a weak reference.
        task.add_done_callback(self.tasks.discard)
        return task

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


def build_services(settings: Settings) -> Services:
    personas = load_personas()
    if settings.vendor_persona not in personas:
        raise ValueError(f"VENDOR_PERSONA {settings.vendor_persona!r} is not one of {sorted(personas)}")
    return Services(
        settings=settings,
        client=CrustdataClient(settings),
        llm=LLM(settings),
        mailer=Mailer(settings),
        slack=SlackNotifier(settings),
        personas=personas,
    )
