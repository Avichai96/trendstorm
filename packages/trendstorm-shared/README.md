# trendstorm-shared

Shared Pydantic models and enum types for TrendStorm AI.

This package is a dependency of both the TrendStorm server and the TrendStorm Python SDK.
It contains the canonical wire-format types for the TrendStorm REST API.

## Install

```bash
pip install trendstorm-shared
```

## Contents

- `trendstorm_shared.types` — `JobStatus`, `SourceType`, `ReportFormat`, `ReviewStatus`, `ReviewDecision`, `StreamEventType`
- `trendstorm_shared.models` — request/response Pydantic v2 models for all API endpoints
