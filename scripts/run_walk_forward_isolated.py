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
    parser = argparse.ArgumentParser(description="Run isolated equity and crypto walk-forward workflows.")
    parser.add_argument("--config", default="configs/edge_quality.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = RobustnessWorkflow(config)
    results = workflow.run_walk_forward_isolated()

    print("Isolated walk-forward artifacts written to artifacts/latest/research/walk_forward_isolated")
    for asset_class, payload in results.items():
        aggregate = payload["aggregate_metrics"]
        print(f"\n[{asset_class}]")
        print(aggregate.to_string(index=False) if not aggregate.empty else "No walk-forward folds were generated.")


if __name__ == "__main__":
    main()
