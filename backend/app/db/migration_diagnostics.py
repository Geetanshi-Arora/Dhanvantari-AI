"""Credential-safe summaries for migration failures."""

import re


def migration_exception_summary(error: BaseException) -> str:
    """Return exception types and SQLSTATE only; never serialize exception text."""
    pending = [error]
    seen = set()
    types = []
    sqlstate = None

    while pending and len(types) < 4:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)

        name = type(current).__name__
        if name not in types:
            types.append(name)

        candidate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if isinstance(candidate, str) and re.fullmatch(r"[0-9A-Z]{5}", candidate):
            sqlstate = candidate

        for linked in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)

    summary = "exception_chain=" + " -> ".join(types or ["UnknownError"])
    if sqlstate:
        summary += f" sqlstate={sqlstate}"
    return summary
