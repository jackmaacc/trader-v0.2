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
    parser = argparse.ArgumentParser(description="Run the trader-v0.2 parameter sweep workflow.")
    parser.add_argument("--config", default="configs/robustness.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = RobustnessWorkflow(config)
    result = workflow.run_parameter_sweep()

    print("Parameter sweep artifacts written to artifacts/latest/research/parameter_sweep")
    summary = result["summary"]
    if isinstance(summary, type(None)) or summary.empty:
        print("No parameter combinations were evaluated.")
    else:
        print(summary.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
