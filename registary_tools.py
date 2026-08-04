import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from gmp_report import get_rows
import requests
from langchain_core.tools import tool

REGISTRY_URL = "https://webnodejs.investorgain.com/cloud/v2/ipo/ipo-url-lists"
IST = ZoneInfo("Asia/Kolkata")
CACHE_TTL = 900  # 15 min; the registry changes rarely

TYPE_ALIASES = {
    "all": "all",
    "both": "all",
    "any": "all",
    "": "all",
    "mainboard": "ipo",
    "main": "ipo",
    "main board": "ipo",
    "ipo": "ipo",
    "sme": "sme",
    "small": "sme",
}

LABEL = {"IPO": "Mainboard", "SME": "SME"}


def resolve_type(ipo_type: str | None) -> str:
    key = (ipo_type or "all").strip().lower()
    if key not in TYPE_ALIASES:
        raise ValueError(
            f"unknown ipo_type {ipo_type!r}; use 'all', 'mainboard', or 'sme'"
        )
    return TYPE_ALIASES[key]


def _slim(r: dict) -> dict:
    """Keep the phase-1 shape, plus the type."""
    return {
        "id": r["id"],
        "name": r["name"],
        "ipo_type": LABEL.get(r.get("category"), r.get("category")),
        "status": r["status"],
        "open_date": r["open_date"],
        "close_date": r["close_date"],
        "days_until_open": r["days_until_open"],
        "days_until_close": r["days_until_close"],
        "cap_price": r["price"],
        "lot_size": r["lot_size"],
        "min_application": r["min_application"],
        "gmp": r["gmp"],
        "gmp_percent": r["gmp_percent"],
        "url": r["url"],
    }


def ist_today() -> date:
    return datetime.now(IST).date()


def _parse_iso_date(value):
    """
    '2026-08-10T00:00:00.000Z' -> date(2026, 8, 10).
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _fetch_registry():
    resp = requests.get(REGISTRY_URL, timeout=15)
    resp.raise_for_status()
    rows = resp.json().get("lists") or []
    return rows


def _normalize(row: dict) -> dict | None:
    open_dt = _parse_iso_date(row.get("issue_open_dt"))
    close_dt = _parse_iso_date(row.get("issue_end_dt"))

    if not open_dt or not close_dt:
        return None

    today = ist_today()
    if today < open_dt:
        status = "upcoming"
    elif today > close_dt:
        status = "closed"
    else:
        status = "open"

    # section = (row.get("urlrewrite_folder_name_main") or "").lower()
    slug = row.get("urlrewrite_folder_name") or ""
    # is_sme = "sme" in section or "sme" in slug

    return {
        "id": row.get("id"),
        "name": row.get("company_short_name"),
        "status": status,
        "open_date": open_dt.isoformat(),
        "close_date": close_dt.isoformat(),
        "days_until_open": (open_dt - today).days,
        "days_until_close": (close_dt - today).days,
        "url": (
            f"https://www.investorgain.com/ipo/{slug}/{row.get('id')}" if slug else None
        ),
    }


def _all_ipos(ipo_type: str = "all") -> list[dict]:
    """Rows for the requested type.

    The report endpoint is month-anchored, so this reads the current month AND
    the next one and merges by id -- otherwise IPOs opening early next month
    are simply invisible, which would quietly break get_upcoming_ipos.
    """
    seg = resolve_type(ipo_type)
    today = ist_today()
    nxt_m = 1 if today.month == 12 else today.month + 1
    nxt_y = today.year + 1 if today.month == 12 else today.year

    merged: dict[int, dict] = {}
    for month, year in ((today.month, today.year), (nxt_m, nxt_y)):
        try:
            for r in get_rows(seg, month=month, year=year):
                merged[r["id"]] = r
        except Exception as e:
            # One month failing shouldn't blank the whole answer.
            return (
                f"DATA_UNAVAILABLE: could not read the IPO source ({type(e).__name__}: {e}).",
            )
    if not merged:
        raise RuntimeError("no data returned for either month")
    return [_slim(r) for r in merged.values()]


def _fail(e: Exception):
    return (
        f"DATA_UNAVAILABLE: could not read the IPO source ({type(e).__name__}: {e}). "
        "Tell the user the data source is unreachable. Do not invent IPO names or dates."
    )


_TYPE_DOC = """
    ipo_type selects the segment: "all" (default), "mainboard" for large
    exchange-listed issues, or "sme" for small and medium enterprise issues.
    Pass "sme" or "mainboard" only when the user asks for that segment.
"""


# --------------------------------------------------------------------------
# tools -- the docstring IS the spec the model sees
# --------------------------------------------------------------------------
@tool
def get_open_ipos(ipo_type: str = "all") -> list[dict] | str:
    """Get IPOs currently open for subscription, that the user can apply to right now.

    Each result has: name, ipo_type, open_date, close_date, and days_until_close,
    where 0 means today is the final day to apply. Sorted most urgent first.
    """
    try:
        rows = [r for r in _all_ipos(ipo_type) if r["status"] == "open"]
    except Exception as e:
        return _fail(e)
    rows.sort(key=lambda r: r["days_until_close"])
    return rows or f"No {_word(ipo_type)}IPOs are open for subscription today."


@tool
def get_upcoming_ipos(ipo_type: str = "all") -> list[dict] | str:
    """Get IPOs whose subscription period has not started yet.

    Each result has: name, ipo_type, open_date, close_date, and days_until_open.
    Sorted soonest first.
    """
    try:
        rows = [r for r in _all_ipos(ipo_type) if r["status"] == "upcoming"]
    except Exception as e:
        return _fail(e)
    rows.sort(key=lambda r: r["days_until_open"])
    return rows or f"No upcoming {_word(ipo_type)}IPOs are listed right now."


@tool
def get_recently_closed_ipos(days: int = 7, ipo_type: str = "all") -> list[dict] | str:
    """Get IPOs that closed for subscription within the last N days (default 7).

    Use for IPOs the user may have missed, or ones now awaiting allotment or listing.
    """
    try:
        rows = [
            r
            for r in _all_ipos(ipo_type)
            if r["status"] == "closed" and -days <= (r["days_until_close"] or 0) < 0
        ]
    except Exception as e:
        return _fail(e)
    rows.sort(key=lambda r: r["days_until_close"], reverse=True)
    return rows or f"No {_word(ipo_type)}IPOs closed in the last {days} days."


@tool
def calculate_ipo_listing_gain(
    ipo_name: str,
    issue_price: float,
    gmp: float,
    lot_size: int,
    lots: int = 1,
) -> dict:
    """
    Calculate estimated IPO listing price, investment amount,
    expected listing gain and expected return percentage.
    """

    estimated_listing_price = issue_price + gmp

    investment = issue_price * lot_size * lots

    estimated_profit = gmp * lot_size * lots

    expected_return_percent = estimated_profit / investment * 100 if investment else 0

    return {
        "ipo_name": ipo_name,
        "issue_price": issue_price,
        "gmp": gmp,
        "estimated_listing_price": estimated_listing_price,
        "lot_size": lot_size,
        "lots": lots,
        "investment": round(investment, 2),
        "estimated_profit": round(estimated_profit, 2),
        "expected_return_percent": round(expected_return_percent, 2),
    }


@tool
def find_ipo_by_name(name: str, ipo_type: str = "all") -> list[dict] | str:
    """Look up a specific IPO by company name or partial name (case-insensitive).

    Use when the user names a company rather than asking for a list.
    Leave ipo_type as "all" unless the user restricted the segment.
    """
    try:
        needle = name.strip().lower()
        rows = [r for r in _all_ipos(ipo_type) if needle in (r["name"] or "").lower()]
    except Exception as e:
        return _fail(e)
    if not rows:
        return f"No IPO found matching '{name}'. Do not guess -- say it was not found."
    rows.sort(key=lambda r: r["open_date"] or "", reverse=True)
    return rows[:10]


def _word(ipo_type: str) -> str:
    """'sme' -> 'SME ' so empty-result messages read naturally."""
    try:
        seg = resolve_type(ipo_type)
    except ValueError:
        return ""
    return {"ipo": "Mainboard ", "sme": "SME ", "all": ""}[seg]


# Append the shared type note to each tool's spec rather than repeating it.
for _t in (
    get_open_ipos,
    get_upcoming_ipos,
    get_recently_closed_ipos,
    find_ipo_by_name,
):
    _t.description = (_t.description or "").rstrip() + "\n" + _TYPE_DOC

IPO_TOOLS = [
    get_open_ipos,
    get_upcoming_ipos,
    get_recently_closed_ipos,
    find_ipo_by_name,
    calculate_ipo_listing_gain,
]
