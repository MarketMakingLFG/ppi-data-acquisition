#!/usr/bin/env python3
from __future__ import annotations

import collect_raw_provider_evidence as collector

PUBLIC_REPOSITORY = "MarketMakingLFG/ppi-data-acquisition"
PUBLIC_REPOSITORY_ID = 1312286476
PUBLIC_CONTRACT_ID = "PPI-R11-PUBLIC-ACQUISITION-003-R2"
COLLECTOR_RELEASE_ID = "PPI-PUBLIC-COLLECTOR-003-R2"


def main() -> int:
    collector.PUBLIC_REPOSITORY = PUBLIC_REPOSITORY
    collector.PUBLIC_REPOSITORY_ID = PUBLIC_REPOSITORY_ID
    collector.PUBLIC_CONTRACT_ID = PUBLIC_CONTRACT_ID
    collector.COLLECTOR_RELEASE_ID = COLLECTOR_RELEASE_ID
    return collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
