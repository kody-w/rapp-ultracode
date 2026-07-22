from __future__ import annotations

import json
from pathlib import Path

from rapp_ultracode.models import Plan


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "schemas" / "rapp-ultracode-plan-1.0.schema.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(Plan.model_json_schema(by_alias=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
