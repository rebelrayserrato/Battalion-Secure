"""Operational entrypoint for GDPR erasure + retention on Battalion-Secure.

Invoked from the main app's erasure fan-out (RAYAAAA-182 ``anonymizeClient``) and
from the retention cron, over the internal docker boundary — e.g.:

    # single matter erasure (Art.17 / matter closure)
    docker compose exec -T battalion \
        python -m review_engine.privacy.erase_cli --matter-id MAT-XXXXXXXXXX

    # retention sweep (daily cron; Counsel 90-day idle backstop, RAYAAAA-195)
    docker compose exec -T battalion \
        python -m review_engine.privacy.erase_cli --retention-sweep

Emits a JSON result on stdout and exits non-zero if ANY residual remains, so the
caller can fail the erasure loudly and retry — no orphaned copies left behind
(RAYAAAA-196 AC2). Uses the same primitives as the verified erase_matter /
sweep_retention. No new network port is opened (matches the RAYAAAA-181/182
`docker compose exec` ops pattern).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from review_engine.privacy.erasure import (
    RETENTION_IDLE_DAYS,
    erase_matter,
    sweep_retention,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erase_cli", description="Battalion-Secure GDPR erasure/retention")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--matter-id", help="erase a single matter across all four stores")
    group.add_argument("--retention-sweep", action="store_true", help="purge matters past the idle backstop")
    parser.add_argument("--idle-days", type=int, default=RETENTION_IDLE_DAYS)
    args = parser.parse_args(argv)

    if args.matter_id:
        report = erase_matter(args.matter_id)
        print(json.dumps({"op": "erase_matter", **asdict(report), "clean": report.clean}, default=str))
        return 0 if report.clean else 2

    result = sweep_retention(idle_days=args.idle_days)
    payload = {
        "op": "retention_sweep",
        "idle_days": result.idle_days,
        "scanned": result.scanned,
        "purged": result.purged_ids,
        "non_clean": result.skipped_unclean,
        "summary": result.summary(),
    }
    print(json.dumps(payload, default=str))
    return 0 if not result.skipped_unclean else 2


if __name__ == "__main__":
    sys.exit(main())
