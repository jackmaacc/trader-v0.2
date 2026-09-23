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
    parser = argparse.ArgumentParser(description="Compare exit profiles across selected state models.")
    parser.add_argument("--config", default="configs/edge_quality.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = RobustnessWorkflow(config)
    result = workflow.run_exit_comparison()

    print("Exit-comparison artifacts written to artifacts/latest/research/exit_comparison")
    summary = result["summary"]
    print(summary.to_string(index=False) if not summary.empty else "No exit combinations were evaluated.")


if __name__ == "__main__":
    main()
