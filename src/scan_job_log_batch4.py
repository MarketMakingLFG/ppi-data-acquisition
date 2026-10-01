#!/usr/bin/env python3
from __future__ import annotations

import scan_job_log

scan_job_log.EXPECTED_WORKFLOW_PATH = ".github/workflows/collect-r11-public-evidence-batch4.yml"

if __name__ == "__main__":
    raise SystemExit(scan_job_log.main())
