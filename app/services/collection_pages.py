"""Opt-in SQL pagination. Internal totals/authorization queries stay complete."""

from contextvars import ContextVar
import json

_context = ContextVar("collection_page", default=None)
DEFAULT_PAGE_SIZE = 2000
HTTP_PAGE_SIZE = 100
MAX_PAGE_SIZE = 10000


def options(collection):
    ctx = _context.get()
    if ctx and ctx["target"] == collection:
        return ctx["offset"], ctx["limit"]
    return 0, HTTP_PAGE_SIZE if ctx else DEFAULT_PAGE_SIZE


def is_selected(collection):
    ctx = _context.get()
    return ctx is not None and ctx["target"] == collection


def result_collection():
    ctx = _context.get()
    path = ctx["path"] if ctx else ""
    return (
        "results:" + path,
        path.rsplit("/", 1)[-1].replace("-", " ").capitalize() or "Results",
    )


def existing_offset(query):
    offset = getattr(query, "_offset_clause", None)
    return int(offset.value) if offset is not None and hasattr(offset, "value") else 0


def note(collection, rows, offset, limit, *, label="Results"):
    ctx = _context.get()
    more = len(rows) > limit
    if ctx is not None:
        ctx["pages"][collection] = {
            "label": label,
            "offset": offset,
            "limit": limit,
            "count": min(len(rows), limit),
            "has_more": more,
            "next_offset": offset + limit if more else None,
        }
    return rows[:limit]


def page_all(query, *, collection, label="Results"):
    offset, limit = options(collection)
    # Full-entity rows use their primary key; DISTINCT/scalar/grouped rows
    # must order by the selected expressions, not an unselected entity id.
    descriptions = query.column_descriptions
    whole = next(
        (
            d.get("entity")
            for d in descriptions
            if d.get("entity") is not None and d.get("expr") is d.get("entity")
        ),
        None,
    )
    if whole is not None and hasattr(whole, "id"):
        query = query.order_by(whole.id.desc())
    else:
        expressions = [
            d["expr"] for d in descriptions if hasattr(d.get("expr"), "desc")
        ]
        if not expressions:
            raise RuntimeError("Paginated query requires a deterministic selected key")
        query = query.order_by(*(expr.desc() for expr in expressions))
    rows = query.offset(offset).limit(limit + 1).all()
    return note(collection, rows, offset, limit, label=label)


async def collection_paging(request, call_next):
    try:
        offset = int(request.query_params.get("collection_offset", "0"))
        limit = int(request.query_params.get("collection_limit", str(HTTP_PAGE_SIZE)))
        if offset < 0 or limit < 1 or limit > MAX_PAGE_SIZE:
            raise ValueError()
    except ValueError:
        from starlette.responses import JSONResponse

        return JSONResponse(
            {"detail": "Invalid collection pagination"}, status_code=422
        )
    ctx = {
        "target": request.query_params.get("collection", ""),
        "offset": offset,
        "limit": limit,
        "pages": {},
        "path": request.url.path,
    }
    token = _context.set(ctx)
    try:
        response = await call_next(request)
        if ctx["pages"]:
            response.headers["X-Collection-Pages"] = json.dumps(
                ctx["pages"], separators=(",", ":")
            )
        return response
    finally:
        _context.reset(token)
