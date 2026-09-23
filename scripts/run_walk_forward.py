from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader_engine.core.config import load_config
from trader_engine.core.logging import configure_logging
from trader_engine.workflows.robustness import RobustnessWorkflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TRADER2.0 walk-forward robustness workflow.")
    parser.add_argument("--config", default="configs/default.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = RobustnessWorkflow(config)
    result = workflow.run_walk_forward()

    print("Walk-forward artifacts written to artifacts/latest/research/walk_forward")
    aggregate = result["aggregate_metrics"]
    if isinstance(aggregate, type(None)) or aggregate.empty:
        print("No walk-forward folds were generated.")
    else:
        print(aggregate.to_string(index=False))


if __name__ == "__main__":
    main()
