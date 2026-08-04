import html
import logging
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

BASE = "https://webnodejs.investorgain.com/cloud/v2/report/data-read"
REPORT_GMP = 331
IST = ZoneInfo("Asia/Kolkata")
CACHE_TTL = 600  # 10 min; GMP updates a few times an hour at most

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.investorgain.com/",
}

# The trailing path segment selects the IPO TYPE. Three values, confirmed:
#   all -> both Mainboard and SME
#   ipo -> Mainboard only
#   sme -> SME only
# This is a type axis only. How the page's status chips (Open, Upcoming, Close,
# Closing Today, Listed, Only Active GMP) are applied is NOT established --
# don't assume either way. The selectors at the bottom of this module compute
# status views locally from rows we already hold, which is correct regardless
# of how the site itself does it.
IPO_TYPES = {"all", "ipo", "sme"}

_TAG = re.compile(r"<[^>]+>")
_BOLD = re.compile(r"<b>(.*?)</b>", re.S)
_LISTED = re.compile(r"L@([\d.]+)\s*\(([-\d.]+)%\)")
_FLAME = "\U0001f525"

_cache: dict[str, tuple[float, dict]] = {}


def ist_today() -> date:
    return datetime.now(IST).date()


def _num(value) -> float | None:
    """'₹3066.89 Cr' / '200.66x' / '--' / '' -> float or None."""
    if value is None:
        return None
    s = _TAG.sub("", html.unescape(str(value)))
    s = (
        s.replace(",", "")
        .replace("\u20b9", "")
        .replace("Cr", "")
        .replace("x", "")
        .strip()
    )
    if s in {"", "-", "--", "NA"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _iso(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def fetch_report(
    ipo_type: str = "mainboard",
    *,
    month: int | None = None,
    year: int | None = None,
    page: int = 1,
    search: str = "",
    force: bool = False,
) -> dict:
    """Raw report payload for one IPO type ("all", "ipo" = Mainboard, "sme").

    NOTE the month/year params: the endpoint is month-anchored. Results spill
    either side of the month (an August call returned rows from 17-Jul), but do
    NOT assume one call sees every IPO. To look further ahead, call again with
    the next month.
    """
    if ipo_type not in IPO_TYPES:
        raise ValueError(
            f"unknown ipo_type {ipo_type!r}; expected one of {sorted(IPO_TYPES)}"
        )

    now = datetime.now(IST)
    month = month or now.month
    year = year or now.year
    # Indian financial year runs April to March.
    fy = (
        f"{year}-{str(year + 1)[-2:]}" if month >= 4 else f"{year - 1}-{str(year)[-2:]}"
    )

    path = f"{BASE}/{REPORT_GMP}/{page}/{month}/{year}/{fy}/0/{ipo_type}"

    # The site sends ?search=&v=HH-MM. Despite Cache-Control: no-store, this
    # endpoint sits behind Cloudflare and will happily serve an hour-old body
    # (Cf-Cache-Status: HIT, Age: 4219). The v param is the cache buster; it
    # changes per minute, so cached edges are bypassed once a minute at most.
    bust = f"{now.hour}-{now.minute}"
    url = f"{path}?search={search}&v={bust}"
    key = f"{path}|{search}"

    hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    payload = resp.json()

    _cache[key] = (time.time(), payload)
    log.info(
        "report %s: %s records, page %s/%s, server time %s",
        ipo_type,
        payload.get("totalRecords"),
        payload.get("iPageNo"),
        payload.get("totalPages"),
        payload.get("currentTime"),
    )
    return payload


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------


def parse_row(row: dict) -> dict:
    """One reportTableData row -> flat clean dict."""
    open_dt = _iso(row.get("~Srt_Open"))
    # Inconsistent prefix in their schema: Srt_ for the others, Str_ for listing.
    close_dt = _iso(row.get("~Srt_Close"))
    boa_dt = _iso(row.get("~Srt_BoA_Dt"))
    listing_dt = _iso(row.get("~Str_Listing") or row.get("~Srt_Listing"))

    today = ist_today()
    if open_dt and today < open_dt:
        status = "upcoming"
    elif close_dt and today > close_dt:
        status = "closed"
    else:
        status = "open"

    # GMP cell: "₹<b>240</b> (27.55%)<br><small><b>146 ↓ / 240 ↑</b></small>"
    # First bold is the current premium, second is the session low/high.
    gmp_raw = html.unescape(str(row.get("GMP") or ""))
    bolds = _BOLD.findall(gmp_raw)
    gmp = _num(bolds[0]) if bolds else None

    low = high = None
    if len(bolds) > 1:
        nums = re.findall(r"-?\d+(?:\.\d+)?", bolds[1])
        if len(nums) >= 2:
            low, high = float(nums[0]), float(nums[1])
    # "0 down / 0 up" means no range on record, not a range of zero.
    if low == 0 and high == 0:
        low = high = None

    # If the premium isn't quoted, the percent is meaningless -- the source
    # sends "0.00", which would otherwise read as "trading flat at par".
    gmp_percent = _num(row.get("~gmp_percent_calc")) if gmp is not None else None

    price = _num(row.get("Price (\u20b9)"))
    lot = _num(row.get("Lot"))

    name_html = html.unescape(str(row.get("Name") or ""))
    listed = _LISTED.search(name_html)

    return {
        "id": row.get("~id"),
        "name": row.get("~ipo_name"),
        "category": row.get("~IPO_Category"),  # "IPO" = Mainboard, "SME" = SME
        "status": status,
        "open_date": open_dt.isoformat() if open_dt else None,
        "close_date": close_dt.isoformat() if close_dt else None,
        "allotment_date": boa_dt.isoformat() if boa_dt else None,
        "listing_date": listing_dt.isoformat() if listing_dt else None,
        "days_until_open": (open_dt - today).days if open_dt else None,
        "days_until_close": (close_dt - today).days if close_dt else None,
        # gmp is None when the grey market isn't quoting yet ("--"). That is NOT
        # the same as zero premium -- keep them distinct.
        "gmp": gmp,
        "gmp_percent": gmp_percent,
        "gmp_low": low,
        "gmp_high": high,
        "price": price,
        "lot_size": int(lot) if lot else None,
        "min_application": round(price * lot) if price and lot else None,
        "issue_size_cr": _num(row.get("IPO Size")),
        "pe_ratio": _num(row.get("~P/E")),
        "subscription_times": _num(row.get("Sub")),
        "rating": html.unescape(str(row.get("Rating") or "")).count(_FLAME) or None,
        "has_anchor": "\u2705" in html.unescape(str(row.get("Anchor") or "")),
        "listed_price": float(listed.group(1)) if listed else None,
        "listing_gain_pct": float(listed.group(2)) if listed else None,
        "updated_at": _TAG.sub(
            "", html.unescape(str(row.get("Updated-On") or ""))
        ).strip()
        or None,
        "url": f"https://www.investorgain.com{row.get('~urlrewrite_folder_name') or ''}",
    }


def get_rows(ipo_type: str = "mainboard", **kw) -> list[dict]:
    payload = fetch_report(ipo_type, **kw)
    rows = payload.get("reportTableData") or []
    out = []
    for raw in rows:
        try:
            out.append(parse_row(raw))
        except Exception as e:
            log.warning("skipping row %s: %s", raw.get("~id"), e)
    return out


def get_rows_tagged(**kw) -> list[dict]:
    """Mainboard and SME in one list, typed by which endpoint returned them.

    Two requests instead of one, but the type is then a fact about membership
    rather than a guess about a field. Use this if ~IPO_Category turns out to
    describe the report rather than the row.
    """
    rows: dict[int, dict] = {}
    for seg, label in (("ipo", "Mainboard"), ("sme", "SME")):
        for r in get_rows(seg, **kw):
            r["ipo_type"] = label
            rows[r["id"]] = r
    return sorted(rows.values(), key=lambda r: r["close_date"] or "9999")


# --------------------------------------------------------------------------
# local selectors -- status views computed from rows we already hold
# --------------------------------------------------------------------------
# These are ours. They intentionally derive status from dates rather than
# trusting any status field or badge, so they hold regardless of how the site
# implements its own chips.


def open_now(rows: list[dict]) -> list[dict]:
    return sorted(
        (r for r in rows if r["status"] == "open"),
        key=lambda r: (
            r["days_until_close"] if r["days_until_close"] is not None else 99
        ),
    )


def upcoming(rows: list[dict]) -> list[dict]:
    return sorted(
        (r for r in rows if r["status"] == "upcoming"),
        key=lambda r: r["days_until_open"] if r["days_until_open"] is not None else 99,
    )


def closing_today(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["days_until_close"] == 0]


def closed(rows: list[dict]) -> list[dict]:
    """Subscription over, not yet listed."""
    return [r for r in rows if r["status"] == "closed" and r["listed_price"] is None]


def listed(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["listed_price"] is not None]


def active_gmp(rows: list[dict]) -> list[dict]:
    """Only IPOs the grey market is actually quoting. gmp is None when there's
    no quote, which is why that distinction was worth preserving."""
    return [r for r in rows if r["gmp"] is not None]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    rows = get_rows("all")

    def show(title, subset):
        print(f"\n{title} ({len(subset)})")
        for r in subset:
            gmp = "--" if r["gmp"] is None else f"{r['gmp']:g}"
            pct = "" if r["gmp_percent"] is None else f"({r['gmp_percent']:.2f}%)"
            print(
                f"  {r['name'][:30]:30} {r['category'] or '?':4} "
                f"close {r['close_date']} GMP {gmp:>7} {pct:>10} "
                f"min {r['min_application'] or '-':>7}"
            )

    show("Open now", open_now(rows))
    show("Closing today", closing_today(rows))
    show("Upcoming", upcoming(rows))
    show("Quoted in grey market", active_gmp(rows))
    show("Already listed", listed(rows))
