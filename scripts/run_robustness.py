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
    parser = argparse.ArgumentParser(description="Run walk-forward and parameter sweep research.")
    parser.add_argument("--config", default="configs/robustness.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = RobustnessWorkflow(config)
    walk_forward = workflow.run_walk_forward()
    parameter_sweep = workflow.run_parameter_sweep()

    print("Walk-forward aggregate metrics:")
    aggregate = walk_forward["aggregate_metrics"]
    print(aggregate.to_string(index=False) if not aggregate.empty else "No walk-forward folds were generated.")
    print()
    print("Top sweep results:")
    summary = parameter_sweep["summary"]
    print(summary.head(10).to_string(index=False) if not summary.empty else "No parameter combinations were evaluated.")


if __name__ == "__main__":
    main()
