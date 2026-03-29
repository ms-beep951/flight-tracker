#!/usr/bin/env python3
"""
DUB -> FAO / FAO -> DUB Flight Price Tracker
Fetches Ryanair prices + sale alerts, saves to prices.json
Run once manually or via scheduled task daily at 9 AM.
"""
import json
import time
import requests
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_FILE = SCRIPT_DIR / "prices.json"

OUTBOUND_DATE = "2026-05-10"   # Wed 10 May 2026
INBOUND_DATE  = "2026-05-16"   # Tue 16 May 2026
ORIGIN        = "DUB"
DESTINATION   = "FAO"
ADULTS        = 2
CHILDREN      = 3   # ages 7, 5, 2  →  all qualify as CHD on Ryanair (age 2–11)

# Baggage assumptions (edit to match your booking)
CHECKED_BAGS         = 2     # 20 kg bags each way (family of 5, ~6 nights)
CHECKED_BAG_FEE_EUR  = 30.0  # conservative per-bag-per-direction price
PRIORITY_ADULTS      = 2     # adults buying Priority + 2 cabin bags
PRIORITY_FEE_EUR     = 10.0  # per adult per direction

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-IE,en;q=0.9",
    "Referer":         "https://www.ryanair.com/",
    "Origin":          "https://www.ryanair.com",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def baggage_cost():
    """Return estimated all-in baggage cost for the round trip."""
    checked = CHECKED_BAGS * CHECKED_BAG_FEE_EUR * 2        # both directions
    priority = PRIORITY_ADULTS * PRIORITY_FEE_EUR * 2       # both directions
    return round(checked + priority, 2)


def make_session():
    s = requests.Session()
    try:
        s.get("https://www.ryanair.com/en/ie", headers=HEADERS, timeout=12)
    except Exception:
        pass
    return s


# ── Ryanair ───────────────────────────────────────────────────────────────────

def fetch_ryanair(session):
    url = "https://www.ryanair.com/api/booking/v4/en-gb/availability"
    params = {
        "ADT": ADULTS,
        "CHD": CHILDREN,
        "DateIn":  INBOUND_DATE,
        "DateOut": OUTBOUND_DATE,
        "Destination": DESTINATION,
        "FlexDaysBeforeIn":  0,
        "FlexDaysBeforeOut": 0,
        "FlexDaysIn":        0,
        "FlexDaysOut":       0,
        "Origin":     ORIGIN,
        "RoundTrip":  "true",
        "ToUs":       "AGREED",
    }
    try:
        r = session.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"fetchStatus": "failed", "reason": str(e)}

    outbound_fare = None
    inbound_fare  = None

    for trip in data.get("trips", []):
        orig = trip.get("origin", "")
        for date_block in trip.get("dates", []):
            for flight in date_block.get("flights", []):
                if flight.get("faresLeft", 0) == 0:
                    continue
                reg = flight.get("regularFare")
                if not reg:
                    continue
                total = sum(
                    f.get("publishedFare", 0) * f.get("count", 0)
                    for f in reg.get("fares", [])
                )
                if orig == ORIGIN and outbound_fare is None:
                    outbound_fare = total
                    break
                elif orig == DESTINATION and inbound_fare is None:
                    inbound_fare = total
                    break

    if outbound_fare is None and inbound_fare is None:
        return {"fetchStatus": "failed", "reason": "No fares returned by API"}

    base  = round((outbound_fare or 0) + (inbound_fare or 0), 2)
    bags  = baggage_cost()
    total = round(base + bags, 2)

    return {
        "fetchStatus":   "ok",
        "baseFare":      base,
        "baggageFees":   bags,
        "baggageNote":   f"€{bags:.2f} (est: {CHECKED_BAGS}×20kg bags + priority x{PRIORITY_ADULTS} adults, both dirs)",
        "totalEUR":      total,
        "outboundFare":  round(outbound_fare or 0, 2),
        "inboundFare":   round(inbound_fare  or 0, 2),
    }


def check_ryanair_sale(session):
    """Check several Ryanair offer/promo endpoints for active sales."""
    endpoints = [
        "https://www.ryanair.com/api/offers/en-gb/promotions",
        "https://www.ryanair.com/api/offers/en-gb/sale",
        "https://www.ryanair.com/api/offers/v2/en-gb/promotions",
    ]
    for url in endpoints:
        try:
            r = session.get(url, headers=HEADERS, timeout=10)
            if r.ok:
                body = r.json()
                if body:
                    # Normalise: list or dict with items
                    items = body if isinstance(body, list) else body.get("promotions", body.get("items", []))
                    if items:
                        names = []
                        for item in (items[:3] if isinstance(items, list) else []):
                            name = item.get("title") or item.get("name") or item.get("promoCode") or ""
                            if name:
                                names.append(name)
                        detail = (", ".join(names) + " — check ryanair.com for details") if names else "Check ryanair.com for details"
                        return {"active": True, "details": detail}
        except Exception:
            continue

    # Fallback: check for route-specific deals on the search page
    try:
        r = session.get(
            "https://www.ryanair.com/api/booking/v4/en-gb/cheapestOnDay",
            params={"market": "en-ie", "departureAirportIataCode": ORIGIN,
                    "currency": "EUR", "language": "en"},
            headers=HEADERS, timeout=10
        )
        if r.ok:
            data = r.json()
            fares = data.get("outbound", {}).get("fares", []) if isinstance(data, dict) else []
            for fare in fares:
                if fare.get("hasPromoDiscount") or fare.get("discountInPercent", 0) > 0:
                    return {"active": True, "details": "Discounted fares available on route — check ryanair.com"}
    except Exception:
        pass

    return {"active": False, "details": "No active Ryanair sales detected"}


# ── Skyscanner ────────────────────────────────────────────────────────────────

def fetch_skyscanner(session):
    """
    Attempt Skyscanner's internal flights-search API.
    Skyscanner aggressively protects their API; this may return partial/no data.
    A manual check via the link in the dashboard is always recommended.
    """
    url = "https://www.skyscanner.net/g/conductor/v1/fps3/search/"
    params = {
        "geo_schema": "skyscanner",
        "cabin_class": "economy",
        "response_include": "query;stats;paging",
    }
    payload = {
        "query": {
            "market":   "IE",
            "locale":   "en-IE",
            "currency": "EUR",
            "query_legs": [
                {
                    "origin_place_id":      {"iata": ORIGIN},
                    "destination_place_id": {"iata": DESTINATION},
                    "date": {"year": 2026, "month": 5, "day": 10},
                },
                {
                    "origin_place_id":      {"iata": DESTINATION},
                    "destination_place_id": {"iata": ORIGIN},
                    "date": {"year": 2026, "month": 5, "day": 16},
                },
            ],
            "adults":        ADULTS,
            "children_ages": [7, 5, 2],
            "cabin_class":   "CABIN_CLASS_ECONOMY",
            "include_unpriced_itineraries": True,
            "include_mixed_booking_options": True,
        }
    }
    sky_headers = {
        **HEADERS,
        "Content-Type": "application/json",
        "Referer": "https://www.skyscanner.net/",
        "Origin":  "https://www.skyscanner.net",
        "x-skyscanner-channel": "website",
    }
    try:
        r = session.post(url, params=params, json=payload, headers=sky_headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        # Try to extract cheapest itinerary price
        stats = data.get("stats", {})
        min_price = None
        if stats:
            itins = stats.get("itineraries", {})
            minv = itins.get("minPrice", {})
            if minv:
                amount = minv.get("amount") or minv.get("price")
                if amount:
                    min_price = float(amount)

        if min_price is not None:
            bags  = baggage_cost()
            total = round(min_price + bags, 2)
            return {
                "fetchStatus": "ok",
                "baseFare":    round(min_price, 2),
                "baggageFees": bags,
                "baggageNote": f"€{bags:.2f} est baggage (check airline for exact fees)",
                "totalEUR":    total,
            }

        return {"fetchStatus": "no_data", "reason": "API responded but no price data extracted"}

    except Exception as e:
        return {"fetchStatus": "failed", "reason": str(e)}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n[{today}] Flight Price Tracker - DUB->FAO {OUTBOUND_DATE} / FAO->DUB {INBOUND_DATE}")
    print(f"Passengers: {ADULTS} adults + {CHILDREN} children\n")

    session = make_session()

    print("  Ryanair availability...", end=" ", flush=True)
    ryanair = fetch_ryanair(session)
    status = ryanair.get("fetchStatus")
    if status == "ok":
        print(f"OK  |  Base: €{ryanair['baseFare']}  Bags: €{ryanair['baggageFees']}  Total: €{ryanair['totalEUR']}")
    else:
        print(f"FAILED — {ryanair.get('reason', '?')}")

    time.sleep(1)

    print("  Ryanair sale check...  ", end=" ", flush=True)
    sale = check_ryanair_sale(session)
    print(f"{'SALE ACTIVE: ' + sale['details'] if sale['active'] else 'No active sales'}")

    time.sleep(1)

    print("  Skyscanner...          ", end=" ", flush=True)
    skyscanner = fetch_skyscanner(session)
    status_sky = skyscanner.get("fetchStatus")
    if status_sky == "ok":
        print(f"OK  |  Base: €{skyscanner['baseFare']}  Total: €{skyscanner['totalEUR']}")
    else:
        print(f"{status_sky.upper()} — {skyscanner.get('reason', '?')}")

    # ── Save to JSON ──────────────────────────────────────────────────────────
    entry = {
        "date":       today,
        "ryanair":    ryanair,
        "skyscanner": skyscanner,
        "saleAlert":  sale,
    }

    existing: list = []
    if DATA_FILE.exists():
        try:
            existing = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    dates = [d.get("date") for d in existing]
    if today in dates:
        existing[dates.index(today)] = entry
        print(f"\n  Updated existing entry for {today}")
    else:
        existing.append(entry)
        print(f"\n  Appended new entry for {today}")

    DATA_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved -> {DATA_FILE}\n")


if __name__ == "__main__":
    main()
