"""Operational CLI.

    python -m awa.cli init-db          # apply migrations
    python -m awa.cli create-admin-key # issue an admin API key
    python -m awa.cli demo             # seed a synthetic multi-client corpus
    python -m awa.cli report STUDY_ID [--failure-rate 5]
    python -m awa.cli serve [--port 8000]
"""

from __future__ import annotations

import argparse
import json

from awa.db import get_engine, run_migrations


def main() -> None:
    parser = argparse.ArgumentParser(prog="awa")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    key = sub.add_parser("create-admin-key")
    key.add_argument("--label", default="admin key")
    demo = sub.add_parser("demo")
    demo.add_argument("--clients", type=int, default=6)
    report = sub.add_parser("report")
    report.add_argument("study_id")
    report.add_argument("--failure-rate", type=float, default=5.0)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    engine = get_engine()
    if args.command == "init-db":
        applied = run_migrations(engine)
        print(f"applied: {applied or 'nothing (up to date)'}")
    elif args.command == "create-admin-key":
        from awa.security import issue_key
        run_migrations(engine)
        print(issue_key(engine, client_id=None, role="admin", label=args.label))
    elif args.command == "demo":
        from awa.synthetic import seed_demo
        run_migrations(engine)
        summary = seed_demo(engine, n_clients=args.clients)
        print(json.dumps(summary, indent=2))
    elif args.command == "report":
        from awa.analysis import cascade, experience, metrics, sizing
        obs = metrics.load_observations(engine, args.study_id)
        out = {
            "desks": metrics.utilisation_summary(obs, "desk"),
            "meeting_rooms": metrics.meeting_room_module(obs),
            "desk_not_found_risk": sizing.desk_not_found_risk(engine, args.study_id),
            "desk_sizing": sizing.desks_required(engine, args.study_id,
                                                 args.failure_rate),
            "headcount_cascade": cascade.headcount_cascade(engine, args.study_id),
            "experience_overlay": experience.experience_overlay(engine,
                                                                args.study_id),
        }
        print(json.dumps(out, indent=2, default=str))
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("awa.api.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
