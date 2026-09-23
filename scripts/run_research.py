from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader_engine.core.config import load_config
from trader_engine.core.logging import configure_logging
from trader_engine.workflows.research import ResearchWorkflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TRADER2.0 research workflow.")
    parser.add_argument("--config", default="configs/default.yaml", help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    configure_logging(config.logging)

    workflow = ResearchWorkflow(config)
    result = workflow.run()

    print(f"Artifacts: {result['artifacts_dir']}")
    print("Top opportunities:")
    opportunities = result["opportunities"]
    if opportunities.empty:
        print("No opportunities generated.")
    else:
        print(
            opportunities[
                ["symbol", "asset_class", "direction", "expected_value", "confidence", "score", "current_state"]
            ]
            .head(15)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
