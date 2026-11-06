"""Runs the real Anthropic SDK against a fake server, so the wire format is checked without a key."""

import json

import anthropic
import httpx2

from app.personas import load_personas
from app.llm import LLM, LLMOutput
from app.models import EvidenceTable
from tests.conftest import make_settings

CRUSTDATA = load_personas()["crustdata"]



def walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from walk_objects(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from walk_objects(item)


async def test_request_shape_on_the_wire(tmp_path):
    seen = {}
    output = {
        "segment": "gtm", "confidence": 0.8, "opening": {"claim": "", "evidence_ids": []}, "subject": "", "picks": [],
        "reasons": [{"claim": "You sell.", "evidence_ids": ["E1"]}],
        "slots": {"target_role": "", "target_city": "", "target_country": "", "icp_industry": "SaaS", "buyer_title": "", "research_field": ""},
    }

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    table = EvidenceTable()
    table.add("person/identify", "basic_profile.current_title", "Founder", "2026-10-20")
    result = await LLM(make_settings(tmp_path), client=client).classify(None, table, "acme.com", CRUSTDATA)

    assert isinstance(result, LLMOutput) and result.segment == "gtm"
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["output_config"]["effort"] == "low"
    assert "temperature" not in body and "thinking" not in body
    schema = body["output_config"]["format"]["schema"]
    for obj in walk_objects(schema):
        assert obj.get("additionalProperties") is False
        assert set(obj["required"]) == set(obj["properties"])
    assert "Founder" in body["messages"][0]["content"]
