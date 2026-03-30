"""Entry point: python -m alpha_lab.dashboard.api"""

import argparse

import uvicorn
from alpha_lab.dashboard.engine.touch_detector import parse_disabled_level_types

parser = argparse.ArgumentParser(description="Trade Dashboard API server")
parser.add_argument(
    "--replay",
    action="store_true",
    help="Start in replay mode (model + accounts, no live data feed)",
)
parser.add_argument(
    "--disable-levels",
    default="",
    help="Comma-separated level types to disable at touch layer (e.g. pdh,pdl)",
)
args = parser.parse_args()
disabled_level_types = parse_disabled_level_types(args.disable_levels)

if args.replay:
    from alpha_lab.dashboard.api.server import create_app, create_replay_ready_state

    state = create_replay_ready_state(
        disabled_level_types=disabled_level_types,
    )
else:
    from alpha_lab.dashboard.api.server import create_app

    state = None  # create_app will call _create_live_state()

uvicorn.run(
    create_app(state=state, disabled_level_types=disabled_level_types),
    host="0.0.0.0",
    port=8000,
)
