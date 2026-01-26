#!/usr/bin/env python3
"""
List free OpenRouter models and show the best ones by a chosen sort key.

"Best" defaults to highest context length. You can change the sort key
using --sort and limit results with --limit.
"""

import argparse
import json
import os
import sys
import urllib.request
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

DEFAULT_URL = "https://openrouter.ai/api/v1/models"


def load_env() -> None:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)


def to_decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def is_free(pricing: dict) -> bool:
    prompt = to_decimal(pricing.get("prompt"))
    completion = to_decimal(pricing.get("completion"))
    request = to_decimal(pricing.get("request"))
    image = to_decimal(pricing.get("image"))

    if prompt is None or completion is None:
        return False
    if prompt != 0 or completion != 0:
        return False
    if request not in (None, Decimal("0")):
        return False
    if image not in (None, Decimal("0")):
        return False
    return True


def fetch_models(api_key: str, url: str) -> list:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/chrishonson/night-shift-agent",
        "X-Title": "night-shift-agent",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("data") or data.get("models") or []


def format_price(value) -> str:
    parsed = to_decimal(value)
    return str(parsed) if parsed is not None else "-"


def sort_models(models: list, sort_key: str) -> list:
    if sort_key == "context_length":
        return sorted(models, key=lambda m: int(m.get("context_length") or 0), reverse=True)
    if sort_key == "created":
        return sorted(models, key=lambda m: int(m.get("created") or 0), reverse=True)
    if sort_key == "name":
        return sorted(models, key=lambda m: (m.get("name") or "").lower())
    if sort_key == "id":
        return sorted(models, key=lambda m: (m.get("id") or "").lower())
    return models


def print_table(models: list) -> None:
    headers = ["id", "ctx", "prompt", "completion", "name"]
    rows = [headers]

    for model in models:
        pricing = model.get("pricing") or {}
        rows.append([
            model.get("id") or "",
            str(model.get("context_length") or 0),
            format_price(pricing.get("prompt")),
            format_price(pricing.get("completion")),
            model.get("name") or "",
        ])

    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    for row_index, row in enumerate(rows):
        line = "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))
        if row_index == 0:
            print(line)
            print("  ".join("-" * widths[i] for i in range(len(headers))))
        else:
            print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List free OpenRouter models and highlight the best ones."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="OpenRouter models endpoint")
    parser.add_argument(
        "--sort",
        choices=["context_length", "created", "name", "id"],
        default="context_length",
        help="Sort key for best-model selection",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of models to show (0 = all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON for the filtered models",
    )
    args = parser.parse_args()

    load_env()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 1

    models = fetch_models(api_key, args.url)
    free_models = [model for model in models if is_free(model.get("pricing") or {})]
    free_models = sort_models(free_models, args.sort)

    limit = args.limit if args.limit > 0 else len(free_models)
    selected = free_models[:limit]

    if args.json:
        print(json.dumps(selected, indent=2))
        return 0

    print(
        f"Found {len(free_models)} free models. Showing {len(selected)} best by {args.sort}."
    )
    print_table(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
