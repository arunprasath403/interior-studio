"""Plan a home and write its room manifest.

Rules only:   python -m engine.plan --budget 40000 --area 90 --bhk 2BHK --style "dark academia"
With agents:  python -m engine.plan --brief "2BHK, 90 sqm, 30 lakh, warm scandinavian, no TV" --profile personal

With --brief, Bedrock (BEDROCK_MODEL_ID, default Mistral Large 3) reads the brief and designs each room.
The rule engine validates everything the model says and still enforces the budget.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .manifest import build_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brief", help="free-text requirements; uses the Bedrock agents")
    parser.add_argument("--profile", help="AWS CLI profile for Bedrock (with --brief)")
    parser.add_argument("--budget", type=float, help="total budget")
    parser.add_argument("--area", type=float, help="total area in square metres")
    parser.add_argument("--bhk", help="e.g. 2BHK")
    parser.add_argument("--style", help="e.g. 'dark academia' or free text")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="out/manifest.json")
    args = parser.parse_args()

    if args.brief:
        from .agents import AgentError, bedrock_llm, plan_from_brief

        try:
            manifest = plan_from_brief(bedrock_llm(args.profile), args.brief, args.seed)
        except AgentError as err:
            raise SystemExit(f"could not plan from the brief: {err}")
    else:
        missing = [name for name in ("budget", "area", "bhk", "style") if getattr(args, name) is None]
        if missing:
            parser.error(f"give --brief, or all of --budget --area --bhk --style (missing: {', '.join(missing)})")
        manifest = build_manifest(args.budget, args.area, args.bhk, args.style, args.currency, args.seed)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    meta = manifest["metadata"]
    print(f"{out}: {len(manifest['topology']['nodes'])} panorama rooms, tier={meta['tier']}, "
          f"design={meta['design_source']}, spent {meta['spent']:g} of {meta['total_budget']:g} {meta['currency']}")


if __name__ == "__main__":
    main()
