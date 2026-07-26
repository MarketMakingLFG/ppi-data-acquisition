#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime
from typing import Any

SUPPORTED_ENTITIES = (
    "AAPL", "MU", "NVDA", "AMD", "AVGO", "INTC",
    "TSM", "ARM", "QCOM", "MRVL", "GFS", "TXN",
)
EXPECTED_YFINANCE_VERSION = "1.5.1"


def normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(child) for child in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return normalize(item())
        except (TypeError, ValueError):
            pass
    try:
        import pandas as pd

        if bool(pd.isna(value)):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch bounded Yahoo Finance expectation tables on a public runner")
    parser.add_argument("--entity", choices=SUPPORTED_ENTITIES, required=True)
    args = parser.parse_args(argv)

    import yfinance as yf

    version = getattr(yf, "__version__", "unknown")
    if version != EXPECTED_YFINANCE_VERSION:
        raise SystemExit(f"unexpected yfinance version: {version}")
    ticker = yf.Ticker(args.entity)
    payload = {
        "entity": args.entity,
        "source": "Yahoo Finance analysis via yfinance",
        "yfinance_version": version,
        "eps_trend": normalize(ticker.get_eps_trend(as_dict=True)),
        "eps_revisions": normalize(ticker.get_eps_revisions(as_dict=True)),
        "earnings_estimate": normalize(ticker.get_earnings_estimate(as_dict=True)),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
