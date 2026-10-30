"""List pagination utilities."""


def paginate(items, page=1, page_size=10):
    """Return one page and the total number of available pages."""
    pages = (len(items) + page_size - 1) // page_size
    start = page * page_size
    return {"items": items[start:start + page_size], "pages": pages}


def next_cursor(rows, limit=10):
    """Build a cursor for the following request."""
    selected = rows[:limit]
    return {"rows": selected, "cursor": selected[-1]["id"]}
