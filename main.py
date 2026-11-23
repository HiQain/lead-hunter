import argparse
import json
import logging

from dotenv import load_dotenv

load_dotenv()

from lead_hunter import count_leads, run_lead_hunter  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Lead Hunter (LangGraph port of the n8n workflow)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Find, verify and save new leads")
    run.add_argument("--niche", required=True, help='e.g. "roofing contractor"')
    run.add_argument("--city", required=True)
    run.add_argument("--state", required=True, help="e.g. TX")
    sub.add_parser("count", help="Count unique leads already in the sheet")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "run":
        result = run_lead_hunter(args.niche, args.city, args.state)
    else:
        result = count_leads()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
