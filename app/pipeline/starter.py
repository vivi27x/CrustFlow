"""Turns Claude's starter spec into SQL that runs.

Claude only fills in a spec: a table, typed columns and invented rows. This module writes the SQL,
so what a signup runs always has the same shape, and text from a public profile can't become a
statement. Anything we can't make safe drops the starter, and the email goes out without it.

The same SQL appears in the email and runs in the sandbox, so the code they read is the code they
see working. Nothing generated ever runs on our server.
"""

from __future__ import annotations

import datetime as dt
import re
import textwrap
from typing import Any

from app.models import COLUMN_TYPES, StarterColumn, StarterSpec
from app.personas import VendorPersona
from app.pipeline.profile_doc import EMAIL, PHONE

IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{1,38}")
RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "both", "case", "cast", "check", "collate",
    "column", "constraint", "create", "current_date", "current_time", "current_timestamp", "current_user", "default",
    "desc", "distinct", "do", "else", "end", "except", "false", "fetch", "for", "foreign", "from", "grant", "group",
    "having", "in", "initially", "intersect", "into", "lateral", "leading", "limit", "localtime", "localtimestamp",
    "not", "null", "offset", "on", "only", "or", "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing", "true", "union", "unique", "user", "using",
    "variadic", "when", "where", "window", "with",
}
OUR_COLUMNS = {"id", "owner_id", "embedding", "created_at"}
MIN_COLUMNS, MAX_COLUMNS = 2, 6
MIN_ROWS, MAX_SEEDS = 3, 4
MAX_TEXT = 160
MAX_PHRASE = 60
MAX_PILLS = 3
MAX_LINE = 72  # What fits in the email's code block without wrapping.

# Sample owners, so row-level security has someone to tell apart. Only the sandbox uses these.
FIRST_USER = "00000000-0000-4000-8000-00000000000a"
OTHER_USER = "00000000-0000-4000-8000-00000000000b"

# The parts of a Supabase project our table relies on. Only the sandbox runs this.
BOOTSTRAP = """
create schema if not exists auth;
create function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create role authenticated nologin;
grant usage on schema public to authenticated;
grant usage on schema auth to authenticated;
alter default privileges in schema public grant all on tables to authenticated;
alter default privileges in schema public grant all on sequences to authenticated;
alter default privileges in schema public grant execute on functions to authenticated;
create publication supabase_realtime;
"""


def _plain(value: str) -> str | None:
    text = " ".join(value.split())
    return text if text.isprintable() else None


def _names_of(person_name: str) -> list[str]:
    return [part for part in re.split(r"[^a-z]+", person_name.lower()) if len(part) > 2]


def _safe_text(value: str, person_name: str) -> bool:
    """Sample rows are invented, so an address, a phone number or their own name means something went wrong."""
    if EMAIL.search(value) or PHONE.search(value):
        return False
    words = set(re.split(r"[^a-z]+", value.lower()))
    return not any(name in words for name in _names_of(person_name))


def _literal(value: str, kind: str, person_name: str) -> str | None:
    """A SQL literal for one value, or None if it isn't really that type."""
    text = _plain(value)
    if text is None or len(text) > MAX_TEXT:
        return None
    if kind == "text":
        return "'" + text.replace("'", "''") + "'" if text and _safe_text(text, person_name) else None
    if kind == "integer":
        return text if re.fullmatch(r"-?\d{1,12}", text) else None
    if kind == "numeric":
        return text if re.fullmatch(r"-?\d{1,12}(\.\d{1,6})?", text) else None
    if kind == "boolean":
        return text.lower() if text.lower() in ("true", "false") else None
    if kind == "timestamptz":
        try:
            return f"'{dt.date.fromisoformat(text[:10]).isoformat()}'"
        except ValueError:
            return None
    return None


def _columns(spec: StarterSpec) -> list[StarterColumn] | None:
    names = [column.name for column in spec.columns]
    valid = all(
        IDENTIFIER.fullmatch(c.name) and c.name not in RESERVED and c.name not in OUR_COLUMNS and c.type in COLUMN_TYPES
        for c in spec.columns
    )
    has_text = any(c.type == "text" for c in spec.columns)
    if not valid or not has_text or len(set(names)) != len(names) or not MIN_COLUMNS <= len(names) <= MAX_COLUMNS:
        return None
    return spec.columns


def _rows(spec: StarterSpec, columns: list[StarterColumn], person_name: str) -> list[list[str]] | None:
    rows = []
    for row in spec.rows:
        if len(row) != len(columns):
            return None
        literals = [_literal(value, column.type, person_name) for value, column in zip(row, columns)]
        if any(literal is None for literal in literals):
            return None
        rows.append(literals)
    return rows if len(rows) >= MIN_ROWS else None


def _phrase(value: str, person_name: str) -> str:
    text = _plain(value) or ""
    return text if text and len(text) <= MAX_PHRASE and _safe_text(text, person_name) else ""


def _values(row: list[str]) -> str:
    return ", ".join(row)


def _fit(*parts: str) -> str:
    """One line if it fits, otherwise a line for each part."""
    line = " ".join(parts)
    return line if len(line) <= MAX_LINE else "\n  ".join(parts)


def _wrapped(values: list[str], indent: str) -> str:
    """Comma-separated, breaking only between values, so a string is never cut in two."""
    lines = [""]
    for value in values:
        joined = f"{lines[-1]}, {value}" if lines[-1] else value
        if lines[-1] and len(indent + joined) > MAX_LINE - 2:  # Room for the closing "),".
            lines[-1] += ","
            lines.append(value)
        else:
            lines[-1] = joined
    return f"\n{indent}".join(lines)


def _table_sql(table: str, columns: list[StarterColumn], vector: bool) -> str:
    lines = [
        "  id bigint generated always as identity primary key",
        "  owner_id uuid default auth.uid()",
        *(f"  {c.name} {c.type} not null" for c in columns),
        *(["  embedding vector(1536)"] if vector else []),
        "  created_at timestamptz not null default now()",
    ]
    body = ",\n".join(lines)
    return f"create table {table} (\n{body}\n);"


def _policy_sql(table: str) -> str:
    return (
        _fit(f"alter table {table}", "enable row level security;") + "\n"
        'create policy "owners manage their rows"\n'
        f"  on {table} for all to authenticated\n"
        "  using (owner_id = auth.uid())\n"
        "  with check (owner_id = auth.uid());"
    )


def _match_sql(table: str) -> str:
    return (
        f"create function match_{table}(\n"
        "  query_embedding vector(1536),\n"
        "  match_count int default 5\n"
        ")\n"
        + _fit(f"returns setof {table}", "language sql stable as $$") + "\n"
        f"  select * from {table}\n"
        "  where embedding is not null\n"
        "  order by embedding <=> query_embedding\n"
        "  limit match_count;\n"
        "$$;"
    )


def _seed_sql(table: str, columns: list[StarterColumn], seeds: list[list[str]]) -> str:
    names = _wrapped([column.name for column in columns], "   ")
    values = ",\n".join(f"  ({_wrapped(row, '   ')})" for row in seeds)
    return f"insert into {table}\n  ({names})\nvalues\n{values};"


def _heading(company: str, vendor: str, purpose: str) -> str:
    title = f"{vendor} starter for {company}" if company else f"{vendor} starter"
    lines = [title, *textwrap.wrap(purpose, MAX_LINE - 3), "Sample rows are invented, and have no owner until you set one."]
    return "\n".join(f"-- {line}" for line in lines)


def _blocks(table: str, columns: list[StarterColumn], seeds: list[list[str]], picks: set[str]) -> list[dict[str, str]]:
    """The SQL in the order it has to run, named so an email can explain each part."""
    vector = "vector" in picks
    extension = "create extension if not exists vector;\n\n" if vector else ""
    index = _fit(f"create index on {table}", "using hnsw (embedding vector_cosine_ops);")
    blocks = [{"id": "table", "code": extension + _table_sql(table, columns, vector)}]
    if vector:
        blocks.append({"id": "vector", "code": f"{index}\n\n{_match_sql(table)}"})
    blocks.append({"id": "policy", "code": _policy_sql(table)})
    if "realtime" in picks:
        blocks.append({"id": "realtime", "code": _fit("alter publication supabase_realtime", f"add table {table};")})
    blocks.append({"id": "seed", "code": _seed_sql(table, columns, seeds)})
    return blocks


def _project_blocks(table: str, picks: set[str]) -> list[dict[str, str]]:
    """Extensions the sandbox's Postgres doesn't have, so these lines only run on a real project."""
    blocks = []
    if "queues" in picks:
        blocks.append({"id": "queues", "code": f"create extension if not exists pgmq;\nselect pgmq.create('{table}_jobs');"})
    if "cron" in picks:
        blocks.append({"id": "cron", "code": (
            "create extension if not exists pg_cron;\n"
            f"select cron.schedule(\n  '{table}_nightly', '0 3 * * *',\n  $$vacuum analyze {table}$$\n);"
        )})
    return blocks


def _mcp_prompt(vendor: str, purpose: str, table: str, columns: list[StarterColumn], picks: set[str]) -> str:
    names = ", ".join(f"{c.name} ({c.type})" for c in columns)
    steps = [
        f"Using the {vendor} MCP server, create a table called {table} for {purpose.rstrip('.').lower()}, "
        f"with these columns: {names}, plus an owner_id for row-level security so each user only sees their own rows.",
    ]
    if "vector" in picks:
        steps.append(f"Add a 1536-dimension embedding column with an HNSW index, and a match_{table} function for similarity search.")
    if "realtime" in picks:
        steps.append("Add the table to the supabase_realtime publication.")
    if "queues" in picks:
        steps.append(f"Create a queue called {table}_jobs.")
    if "cron" in picks:
        steps.append(f"Schedule a nightly vacuum analyze on {table}.")
    return " ".join(steps)


def _filter_pill(table: str, columns: list[StarterColumn], seeds: list[list[str]], listing: str) -> dict[str, Any] | None:
    """Narrows to a value from the sample rows. One that repeats is a category, so it makes a real filter."""
    row = seeds[0]
    repeats = [i for i in range(len(columns)) if sum(seed[i] == row[i] for seed in seeds) > 1]
    ranked = [i for i, c in enumerate(columns) if c.type == "boolean"]
    ranked += [i for i in repeats if columns[i].type == "text"]
    ranked += [i for i, c in enumerate(columns) if c.type == "text" and len(row[i]) <= 26]
    ranked += [i for i, c in enumerate(columns) if c.type in ("integer", "numeric")]
    if not ranked:
        return None
    index = ranked[0]
    name, value = columns[index].name, row[index]
    return {
        "id": "filter", "kind": "filter", "label": f"Only {name} = {value.strip(chr(39))}",
        "hint": "A plain filter, so you can see the policy still applies.",
        "sql": f"select {listing} from {table} where {name} = {value} order by created_at desc limit 5",
    }


def _pills(
    table: str, columns: list[StarterColumn], rows: list[list[str]], seeds: list[list[str]], other_search: str, vector: bool
) -> list[dict[str, Any]]:
    """Three buttons, each changing one thing, so the second run teaches something."""
    names = ", ".join(c.name for c in columns)
    pills: list[dict[str, Any]] = []
    if vector and other_search:
        pills.append({
            "id": "search", "kind": "search", "search": other_search, "label": f"Search: {other_search}",
            "hint": "Runs the same function with a new phrase.",
        })
    pills.append({
        "id": "other_user", "kind": "user", "user": OTHER_USER, "label": "View as another user",
        "hint": "Same query, different signed-in user. The policy hides the rows that aren't theirs.",
    })
    if len(rows) > len(seeds):
        pills.append({
            "id": "add_row", "kind": "insert", "label": "Add a row",
            "hint": "Inserts one more row as the signed-in user, then reruns the query.",
            "sql": f"insert into {table} ({names}) values ({_values(rows[len(seeds)])})",
        })
    if len(pills) < MAX_PILLS:
        pill = _filter_pill(table, columns, seeds, names)
        if pill:
            pills.append(pill)
    return pills[:MAX_PILLS]


def compile_starter(
    spec: StarterSpec | None, picks: list[str], persona: VendorPersona, company: str, person_name: str
) -> dict[str, Any] | None:
    """The starter for this signup, or None if Claude's spec can't be made safe."""
    if spec is None or not persona.starter:
        return None
    table = spec.table
    purpose = _plain(spec.purpose) or ""
    columns = _columns(spec)
    if not IDENTIFIER.fullmatch(table) or table in RESERVED or not purpose or columns is None:
        return None
    if len(purpose) > MAX_TEXT or not _safe_text(purpose, person_name):
        return None
    rows = _rows(spec, columns, person_name)
    if rows is None:
        return None

    chosen = set(picks)
    vector = "vector" in chosen
    search = _phrase(spec.search, person_name) or purpose[:MAX_PHRASE]
    seeds = rows[:min(MAX_SEEDS, max(MIN_ROWS, len(rows) - 1))]
    names = ", ".join(c.name for c in columns)
    text_columns = ", ".join(c.name for c in columns if c.type == "text")
    similarity = "round((1 - (embedding <=> $1::vector))::numeric, 3) as similarity"
    read = (
        f"select {names}, {similarity} from match_{table}($1::vector, 5)" if vector
        else f"select {names} from {table} order by created_at desc limit 5"
    )
    blocks = _blocks(table, columns, seeds, chosen)
    project_blocks = _project_blocks(table, chosen)
    return {
        "table": table,
        "purpose": purpose,
        "company": company,
        "sql": "\n\n".join([_heading(company, persona.name, purpose), *(b["code"] for b in blocks)]),
        "blocks": blocks,
        "project_blocks": project_blocks,
        "project_only_sql": "\n\n".join(b["code"] for b in project_blocks),
        "mcp_prompt": _mcp_prompt(persona.name, purpose, table, columns, chosen),
        "vector": vector,
        "realtime": "realtime" in chosen,
        "bootstrap": BOOTSTRAP,
        "assign_owners": (  # The last sample row goes to the second user, so the policy has something to hide.
            f"update {table} set owner_id = case when id = (select max(id) from {table}) "
            f"then '{OTHER_USER}'::uuid else '{FIRST_USER}'::uuid end"
        ),
        "user": FIRST_USER,
        "search": search,
        "read": read,
        "unembedded": f"select id, concat_ws(' ', {text_columns}) as text from {table} where embedding is null" if vector else "",
        "set_embedding": f"update {table} set embedding = $1::vector where id = $2" if vector else "",
        "pills": _pills(table, columns, rows, seeds, _phrase(spec.other_search, person_name), vector),
    }
