"""The only path to Crustdata.

Every call goes through `CrustdataClient.post`, which applies the per-endpoint limiter,
retries, the credit budget, caching and logging. The simulator plugs in underneath as an
httpx transport, so sim, live and replay all run this same code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app import db, events
from app.config import Settings, get_settings

MAX_RETRIES = 3
MAX_WAIT_S = 10.0

# Requests per minute. Autocomplete is 300 on one docs page and 45 on another; use the lower.
RATE_LIMITS = {
    "/company/identify": 30,
    "/company/enrich": 15,
    "/company/search": 30,
    "/company/search/autocomplete": 300,
    "/person/identify": 30,
    "/person/enrich": 15,
    "/person/search": 30,
    "/person/search/autocomplete": 300,
    "/job/search": 30,
    "/job/search/autocomplete": 45,
    "/web/search/live": 10,
    "/web/enrich/live": 10,
    "/watch/person": 10,  # Each watch path has its own budget, ids and all.
    "/watch/company": 10,
}
DEFAULT_RATE = 30


class CrustdataError(Exception):
    def __init__(self, status: int | None, error_type: str, message: str) -> None:
        super().__init__(f"{status} {error_type}: {message}")
        self.status = status
        self.error_type = error_type
        self.message = message


class PermissionDenied(CrustdataError):
    """403 permission_error. The same body fails again, so it is never retried."""


class OutOfCredits(CrustdataError):
    """insufficient_credits, sent as 402 or 403 depending on the endpoint."""


class Unavailable(CrustdataError):
    """429, 5xx, timeout or network failure that survived every retry."""


class BudgetExceeded(Exception):
    """The next call would break the signup's cap, or the global kill switch."""


@dataclass
class Ledger:
    """Credits spent on one signup, summed from X-Credits-Used."""

    cap: float
    spent: float = 0.0

    def can_spend(self, estimate: float) -> bool:
        return self.spent + estimate <= self.cap + 1e-9

    def charge(self, credits: float) -> None:
        self.spent = round(self.spent + credits, 4)

    def raise_cap(self, cap: float) -> None:
        self.cap = max(self.cap, cap)

    @property
    def remaining(self) -> float:
        return max(0.0, self.cap - self.spent)


@dataclass
class RunContext:
    signup_id: str | None
    ledger: Ledger
    kind: str = "crustdata"  # "sandbox" keeps pill spend apart from the signup's cap


@dataclass
class Response:
    status: int
    data: Any
    credits: float
    latency_ms: int
    cached: bool = False


class SlidingWindow:
    """At most `limit` starts in any 60 seconds. Crustdata's own limit is a sliding window."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.starts: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.starts and now - self.starts[0] >= 60:
                    self.starts.popleft()
                if len(self.starts) < self.limit:
                    self.starts.append(now)
                    return
                await asyncio.sleep(60 - (now - self.starts[0]) + 0.01)


@dataclass
class Faults:
    """Forced failures, for drills and the dashboard's failure toggle."""

    queued: dict[str, list[int]] = field(default_factory=dict)

    def add(self, path: str, status: int, times: int = 1) -> None:
        self.queued.setdefault(path, []).extend([status] * times)

    def next(self, path: str) -> int | None:
        statuses = self.queued.get(path)
        return statuses.pop(0) if statuses else None

    def clear(self) -> None:
        self.queued.clear()


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def body_hash(path: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(f"{path}|{_canonical(body)}".encode()).hexdigest()[:24]


def _fault_response(status: int) -> httpx.Response:
    kinds = {
        402: "insufficient_credits",
        403: "permission_error",
        429: "rate_limit_error",
        500: "internal_error",
        503: "service_unavailable",
    }
    return httpx.Response(
        status,
        json={"error": {"type": kinds.get(status, "invalid_request"), "message": "Forced failure", "metadata": {}}},
        headers={"x-ratelimit-reset": "0"} if status == 429 else {},
    )


class CrustdataClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings or get_settings()
        if transport is None and self.settings.uses_simulator:
            from app.crustdata.simulator import SimulatorTransport

            transport = SimulatorTransport(latency=self.settings.sim_latency)
        self.transport = transport  # The dashboard's "simulate activity" reaches the simulator through this.
        self.http = httpx.AsyncClient(
            base_url=self.settings.crustdata_base_url,
            timeout=self.settings.crustdata_timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self.settings.crustdata_api_key}",
                "x-api-version": self.settings.crustdata_api_version,
                "content-type": "application/json",
            },
        )
        self.limiters: dict[str, SlidingWindow] = {}
        self.faults = Faults()

    async def close(self) -> None:
        await self.http.aclose()

    # ---- public ----

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        ctx: RunContext,
        *,
        estimate: float = 0.0,
        cache: bool = False,
        note: str = "",
    ) -> Response:
        """Raises BudgetExceeded before spending, or a CrustdataError after retries."""
        # Simulated answers must never serve a live run that shares this database.
        source = "sim" if self.settings.uses_simulator else "real"
        cache_key = f"cd:{source}:{body_hash(path, body)}"
        if cache:
            hit = db.cache_get(cache_key)
            if hit is not None:
                self._log(ctx, path, 200, 0, 0.0, cached=True, note=note or "cache hit, 0 credits")
                return Response(200, hit, 0.0, 0, cached=True)

        if estimate > 0:
            if not ctx.ledger.can_spend(estimate):
                self._log(
                    ctx, path, None, None, None,
                    note=f"blocked: {estimate:.2f} would pass the {ctx.ledger.cap:g}-credit cap",
                )
                raise BudgetExceeded(path)
            if db.total_credits() + estimate > self.settings.global_credit_cap:
                self._log(ctx, path, None, None, None, note="blocked: global credit cap reached")
                raise BudgetExceeded(path)

        response = await self._request_with_retries(path, body, ctx, note)
        if cache and response.status == 200:
            db.cache_set(cache_key, response.data)
        return response

    async def get(self, path: str, ctx: RunContext, *, params: dict[str, Any] | None = None, note: str = "") -> Response:
        """Free reads, such as a watch's runs. Never cached: the answer changes on Crustdata's side."""
        if params:
            path = f"{path}?{urlencode(params)}"
        return await self._request_with_retries(path, {}, ctx, note, method="GET")

    async def delete(self, path: str, ctx: RunContext, *, note: str = "") -> Response:
        return await self._request_with_retries(path, {}, ctx, note, method="DELETE")

    # ---- internals ----

    def _limiter(self, path: str) -> SlidingWindow:
        key = limit_key(path)
        if key not in self.limiters:
            self.limiters[key] = SlidingWindow(RATE_LIMITS.get(key, DEFAULT_RATE))
        return self.limiters[key]

    async def _send(self, path: str, body: dict[str, Any], method: str = "POST") -> httpx.Response:
        forced = self.faults.next(limit_key(path))
        if forced is not None:
            return _fault_response(forced)
        mode = self.settings.crustdata_mode
        if mode == "replay":
            return self._load_recording(f"{method} {path}" if method != "POST" else path, body)
        if method == "POST":
            response = await self.http.post(path, json=body)
        else:
            response = await self.http.request(method, path)
        if mode == "record":
            self._save_recording(f"{method} {path}" if method != "POST" else path, body, response)
        return response

    def _recording_path(self, path: str, body: dict[str, Any]) -> Path:
        return Path(self.settings.replay_dir) / f"{body_hash(path, body)}.json"

    def _save_recording(self, path: str, body: dict[str, Any], response: httpx.Response) -> None:
        target = self._recording_path(path, body)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "path": path,
                    "status": response.status_code,
                    "credits": response.headers.get("x-credits-used"),
                    "body": response.json() if response.content else None,
                }
            )
        )

    def _load_recording(self, path: str, body: dict[str, Any]) -> httpx.Response:
        target = self._recording_path(path, body)
        if not target.exists():
            return httpx.Response(
                503,
                json={"error": {"type": "replay_miss", "message": f"No recording for {path}", "metadata": {}}},
            )
        saved = json.loads(target.read_text())
        headers = {"x-credits-used": saved["credits"]} if saved.get("credits") is not None else {}
        return httpx.Response(saved["status"], json=saved["body"], headers=headers)

    async def _request_with_retries(
        self, path: str, body: dict[str, Any], ctx: RunContext, note: str, *, method: str = "POST"
    ) -> Response:
        last_error: CrustdataError | None = None
        for attempt in range(MAX_RETRIES + 1):
            await self._limiter(path).acquire()
            started = time.perf_counter()
            try:
                response = await self._send(path, body, method)
            except httpx.TimeoutException:
                self._log(ctx, path, None, self._ms(started), None, note=f"timeout, attempt {attempt + 1}")
                last_error = Unavailable(None, "timeout", "Request timed out")
                await self._backoff(attempt, None)
                continue
            except httpx.HTTPError as exc:
                self._log(ctx, path, None, self._ms(started), None, note=f"network error: {type(exc).__name__}")
                last_error = Unavailable(None, "network_error", str(exc))
                await self._backoff(attempt, None)
                continue

            latency = self._ms(started)
            credits = _credits(response)  # None on 429s: the header is absent there.
            if credits:
                ctx.ledger.charge(credits)

            if 200 <= response.status_code < 300:  # A cancelled watch answers 204 with no body.
                self._log(ctx, path, response.status_code, latency, credits, note=note)
                return Response(200, response.json() if response.content else None, credits or 0.0, latency)

            error_type, message = _error_details(response)
            detail = f"{error_type}: {message}"[:160]
            self._log(ctx, path, response.status_code, latency, credits, note=detail)

            if error_type == "insufficient_credits":
                raise OutOfCredits(response.status_code, error_type, message)
            if error_type == "replay_miss":  # Retrying a missing recording can't help.
                raise CrustdataError(response.status_code, error_type, message)
            if response.status_code == 403:
                raise PermissionDenied(403, error_type, message)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = Unavailable(response.status_code, error_type, message)
                await self._backoff(attempt, response)
                continue
            raise CrustdataError(response.status_code, error_type, message)

        assert last_error is not None
        raise last_error

    async def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        if attempt >= MAX_RETRIES:
            return
        wait = 0.4 * (2**attempt)
        if response is not None:
            for header in ("retry-after", "x-ratelimit-reset"):
                value = response.headers.get(header)
                if value:
                    try:
                        wait = max(wait if header == "retry-after" else 0.0, float(value))
                    except ValueError:
                        pass  # Retry-After can be an HTTP date; the backoff still applies.
                    break
        await asyncio.sleep(min(MAX_WAIT_S, wait) + random.uniform(0, 0.25))

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _log(
        self,
        ctx: RunContext,
        path: str,
        status: int | None,
        latency_ms: int | None,
        credits: float | None,
        *,
        cached: bool = False,
        note: str = "",
    ) -> None:
        entry = db.log_call(
            signup_id=ctx.signup_id,
            kind=ctx.kind,
            endpoint=path,
            status=status,
            latency_ms=latency_ms,
            credits=credits,
            cached=cached,
            note=note,
        )
        events.publish("call", entry)


def limit_key(path: str) -> str:
    """Rate limits and failure drills are per endpoint, and a watch's ids are part of its path."""
    path = path.split("?")[0]
    if path.startswith("/watch/"):
        return "/".join(path.split("/")[:3])
    return path


def _credits(response: httpx.Response) -> float | None:
    value = response.headers.get("x-credits-used")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        return "unknown", response.text[:120]
    if isinstance(error, str):
        return "unknown", error
    return error.get("type", "unknown"), error.get("message", "")
