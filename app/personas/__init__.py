"""Who the agent works for.

A persona describes the vendor using CrustFlow, not the person signing up: what it sells, who it's
for, and what to look for in a signup's profile. Claude reads it to decide what each signup needs.

A segment with a Crustdata `playbook` runs a real first search. Any other segment gets a tailored
message, because a search only makes sense when the vendor sells data.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from app.models import Playbook

PERSONA_DIR = Path(__file__).resolve().parent
UNKNOWN = "unknown"


class Service(BaseModel):
    """One thing the vendor offers. Claude picks from these, and the link comes from here, never from Claude."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    what: str
    good_for: str
    url: str


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str  # Shown on the "What are you building?" buttons.
    hint: str = ""  # One line under the button.
    look_for: str  # Signals in a person's or company's profile that mean this segment.
    first_value: str  # What a useful first message gives them.
    playbook: Playbook = Playbook.MESSAGE
    sales_alert: bool = False
    button: bool = True
    cta_label: str = ""
    cta_url: str = ""


class VendorPersona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    one_liner: str
    product: str
    not_a_fit: str
    own_domains: list[str]
    competitor_domains: list[str] = []
    sample: bool = False  # Mimicking a real company for a demo. Emails say so.
    cta_label: str = ""
    cta_url: str = ""
    starter: bool = False  # Its signups can run a Postgres starter, so Claude writes a spec for one.
    services: list[Service] = []
    segments: list[Segment]

    @model_validator(mode="after")
    def _check(self) -> "VendorPersona":
        ids = [segment.id for segment in self.segments]
        if not ids:
            raise ValueError(f"persona {self.id} has no segments")
        if len(ids) != len(set(ids)):
            raise ValueError(f"persona {self.id} repeats a segment id")
        if UNKNOWN in ids:
            raise ValueError(f"persona {self.id}: '{UNKNOWN}' is reserved")
        service_ids = [service.id for service in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError(f"persona {self.id} repeats a service id")
        return self

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def segment(self, segment_id: str) -> Segment | None:
        return next((s for s in self.segments if s.id == segment_id), None)

    def has(self, segment_id: str) -> bool:
        return self.segment(segment_id) is not None

    def buttons(self) -> dict[str, str]:
        return {s.id: s.label for s in self.segments if s.button}

    @property
    def runs_searches(self) -> bool:
        return any(s.playbook is not Playbook.MESSAGE for s in self.segments)

    def segment_ids(self) -> list[str]:
        return [*(s.id for s in self.segments), UNKNOWN]


def load_personas(directory: Path = PERSONA_DIR) -> dict[str, VendorPersona]:
    """Runs at startup, so a bad file stops the app here rather than in the middle of a demo."""
    personas = {}
    for path in sorted(directory.glob("*.json")):
        persona = VendorPersona.model_validate(json.loads(path.read_text()))
        personas[persona.id] = persona
    return personas
