"""Entry point: python -m alpha_lab.dashboard.api"""

import argparse

import uvicorn

parser = argparse.ArgumentParser(description="Trade Dashboard API server")
parser.add_argument(
    "--replay",
    action="store_true",
    help="Start in replay mode (model + accounts, no live data feed)",
)
args = parser.parse_args()

if args.replay:
    from alpha_lab.dashboard.api.server import create_app, create_replay_ready_state

    state = create_replay_ready_state()
else:
    from alpha_lab.dashboard.api.server import create_app

    state = None  # create_app will call _create_live_state()

uvicorn.run(create_app(state=state), host="0.0.0.0", port=8000)
