"""Command line.

    python -m profile_agent --email ada@example.edu --name "Ada Example"
"""

from __future__ import annotations

import argparse
import asyncio

from profile_agent.agent import ProfileAgent
from profile_agent.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="profile_agent", description="Build user.md for a signup from their name and email")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name")
    parser.add_argument("--no-cache", action="store_true", help="Fetch company data fresh instead of using the cache")
    parser.add_argument("--max-usd", type=float, help="Claude spend cap for this run (default: 5)")
    args = parser.parse_args()

    config = settings(max_usd=args.max_usd, use_cache=False if args.no_cache else None)
    agent = ProfileAgent(config, args.email, args.name)
    print(f"Profiling {args.email} (research {config.research_model}, write {config.write_model})")
    run = asyncio.run(agent.run())
    print(f"\n{run['status']} in {run['seconds']}s {run['timings']} · {run['crustdata_credits']} Crustdata credits · ${run['claude_usd'] or 0:.2f} Claude")
    print(f"user.md: {run['user_md']}" if run["user_md"] else f"Error: {run['error']}")


if __name__ == "__main__":
    main()
