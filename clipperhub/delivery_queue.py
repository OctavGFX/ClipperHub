"""Choose one immutable daily batch; the dashboard owns dispatch and status."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import local_ai


def daily_batch(settings, now: datetime, rows) -> list[str]:
    # Build naive wall-clock cutoffs then convert each independently, including DST days.
    local_now = now.astimezone()
    hour, minute = map(int, settings.top_time.split(":"))
    end = datetime.combine(local_now.date(), datetime.min.time()).replace(hour=hour, minute=minute)
    if local_now.timestamp() < end.timestamp():
        return []
    folder = Path(settings.data_dir) / "delivery"
    plan = folder / f"{end.date()}.json"
    if plan.is_file():
        return json.loads(plan.read_text(encoding="utf-8"))
    start = end - timedelta(days=1)
    start_ts, end_ts = start.timestamp(), end.timestamp()
    candidates = []
    for row in rows():
        q = row["quality_review"]
        score = q.get("postworthy")
        status = (row["posted"] or {}).get("status")
        if (row["source_type"] != "live" or not row["has_video"] or row["posting_now"]
                or row["schedule"] or status not in (None, "held")
                or row.get("editorial_feedback") == "reject"
                or not start_ts < row["created"] <= end_ts
                or (settings.skip_spoilers and row["text"].get("spoiler"))
                or q.get("status") != "ok" or q.get("review_type") != local_ai.RUBRIC
                or q.get("verified") is not True
                or q.get("model") not in {settings.local_model, settings.local_vision_model} - {""}
                or type(score) not in (int, float) or not settings.top_min_quality <= score <= 10):
            continue
        candidates.append((score, row["created"], row["id"]))
    candidates.sort(reverse=True)
    chosen = [cid for _, _, cid in candidates[:settings.top_n]]
    folder.mkdir(parents=True, exist_ok=True)
    tmp = plan.with_suffix(".tmp")
    tmp.write_text(json.dumps(chosen), encoding="utf-8")
    tmp.replace(plan)
    return chosen
