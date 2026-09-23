from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader_engine.ui.dashboard import render_dashboard


render_dashboard(Path(os.environ.get("TRADER_ARTIFACTS", "artifacts/latest")))
