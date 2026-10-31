"""Apify entry point of lerider/eu-tin-validator.  Local run:  python3 -m eu_ids.tin_actor input.json"""
import json
import sys

from .common import run_actor
from .tin import EVENT, run_batch

if __name__ == "__main__":
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            items, n = run_batch(json.load(fh))
        print(json.dumps({"items": items, "event": EVENT, "chargeable_events": n}, indent=2, ensure_ascii=False))
    else:
        import asyncio

        asyncio.run(run_actor(run_batch, EVENT, "TINs"))
