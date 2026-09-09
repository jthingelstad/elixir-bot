"""Pure parsing shared by event projection and its receipt-based migration."""


def event_snapshot_items(payload) -> list[dict] | None:
    """An empty list is authoritative; an invalid snapshot changes nothing."""
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            return None
        if not any(
            isinstance(item.get(k), str) and item[k].strip() for k in ("eventTag", "title", "name")
        ):
            return None
        if any(
            item.get(k) is not None and not isinstance(item[k], str)
            for k in ("eventTag", "title", "name", "description")
        ):
            return None
        if item.get("gameMode") is not None and not isinstance(item["gameMode"], dict):
            return None
    return items


def event_source_key(item: dict) -> str:
    return next(
        item[k].strip()
        for k in ("eventTag", "title", "name")
        if isinstance(item.get(k), str) and item[k].strip()
    )
