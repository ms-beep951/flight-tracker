#!/usr/bin/env python3
"""
DUB -> FAO / FAO -> DUB Flight Price Tracker
Fetches Ryanair prices, sale alerts, and market indicators, saves to prices.json.
Run once manually or via scheduled task daily at 9 AM.
"""
import json
import os
import time
import requests
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_FILE  = SCRIPT_DIR / "prices.json"

OUTBOUND_DATE = "2026-06-11"   # Thu 11 Jun 2026
INBOUND_DATE  = "2026-06-16"   # Tue 16 Jun 2026
ORIGIN        = "DUB"
DESTINATION   = "FAO"
ADULTS        = 2
CHILDREN      = 3   # ages 7, 5, 2  →  all qualify as CHD on Ryanair (age 2–11)

FLIGHT_DATE = datetime(2026, 6, 11)   # outbound — used for countdown + holiday check

# Baggage assumptions (edit to match your booking)
CHECKED_BAGS        = 2     # 20 kg bags each way (family of 5, ~5 nights)
CHECKED_BAG_FEE_EUR = 30.0  # conservative per-bag-per-direction
PRIORITY_ADULTS     = 2     # adults buying Priority + 2 cabin bags
PRIORITY_FEE_EUR    = 10.0  # per adult per direction

# API keys — set as environment variables or GitHub Secrets
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

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

# Irish school holiday periods 2026 relevant to this flight
SUMMER_START = datetime(2026, 6, 26)
SUMMER_END   = datetime(2026, 9,  4)
EASTER_START = datetime(2026, 4,  2)
EASTER_END   = datetime(2026, 4, 17)


# ── Helpers ───────────────────────────────────────────────────────────────────

def baggage_cost():
    checked  = CHECKED_BAGS * CHECKED_BAG_FEE_EUR * 2
    priority = PRIORITY_ADULTS * PRIORITY_FEE_EUR * 2
    return round(checked + priority, 2)


def make_session():
    s = requests.Session()
    try:
        s.get("https://www.ryanair.com/en/ie", headers=HEADERS, timeout=12)
    except Exception:
        pass
    return s


# ── Ryanair flight price ───────────────────────────────────────────────────────

def fetch_ryanair(session):
    url = "https://www.ryanair.com/api/booking/v4/en-gb/availability"
    params = {
        "ADT": ADULTS,
        "CHD": CHILDREN,
        "DateIn":            INBOUND_DATE,
        "DateOut":           OUTBOUND_DATE,
        "Destination":       DESTINATION,
        "FlexDaysBeforeIn":  0,
        "FlexDaysBeforeOut": 0,
        "FlexDaysIn":        0,
        "FlexDaysOut":       0,
        "Origin":    ORIGIN,
        "RoundTrip": "true",
        "ToUs":      "AGREED",
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
        "fetchStatus":  "ok",
        "baseFare":     base,
        "baggageFees":  bags,
        "baggageNote":  f"€{bags:.2f} (est: {CHECKED_BAGS}×20kg bags + priority x{PRIORITY_ADULTS} adults, both dirs)",
        "totalEUR":     total,
        "outboundFare": round(outbound_fare or 0, 2),
        "inboundFare":  round(inbound_fare  or 0, 2),
    }


def check_ryanair_sale(session):
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
                    items = body if isinstance(body, list) else body.get("promotions", body.get("items", []))
                    if items:
                        names = []
                        for item in (items[:3] if isinstance(items, list) else []):
                            name = item.get("title") or item.get("name") or item.get("promoCode") or ""
                            if name:
                                names.append(name)
                        detail = (", ".join(names) + " — check ryanair.com") if names else "Check ryanair.com for details"
                        return {"active": True, "details": detail}
        except Exception:
            continue

    try:
        r = session.get(
            "https://www.ryanair.com/api/booking/v4/en-gb/cheapestOnDay",
            params={"market": "en-ie", "departureAirportIataCode": ORIGIN, "currency": "EUR", "language": "en"},
            headers=HEADERS, timeout=10,
        )
        if r.ok:
            data = r.json()
            fares = data.get("outbound", {}).get("fares", []) if isinstance(data, dict) else []
            for fare in fares:
                if fare.get("hasPromoDiscount") or fare.get("discountInPercent", 0) > 0:
                    return {"active": True, "details": "Discounted fares on route — check ryanair.com"}
    except Exception:
        pass

    return {"active": False, "details": "No active Ryanair sales detected"}


# ── Skyscanner ────────────────────────────────────────────────────────────────

def fetch_skyscanner(session):
    url = "https://www.skyscanner.net/g/conductor/v1/fps3/search/"
    params = {
        "geo_schema":       "skyscanner",
        "cabin_class":      "economy",
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
                    "date": {"year": 2026, "month": 6, "day": 11},
                },
                {
                    "origin_place_id":      {"iata": DESTINATION},
                    "destination_place_id": {"iata": ORIGIN},
                    "date": {"year": 2026, "month": 6, "day": 16},
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
        "Content-Type":          "application/json",
        "Referer":               "https://www.skyscanner.net/",
        "Origin":                "https://www.skyscanner.net",
        "x-skyscanner-channel":  "website",
    }
    try:
        r = session.post(url, params=params, json=payload, headers=sky_headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        stats     = data.get("stats", {})
        min_price = None
        if stats:
            itins = stats.get("itineraries", {})
            minv  = itins.get("minPrice", {})
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
                "baggageNote": f"€{bags:.2f} est baggage",
                "totalEUR":    total,
            }

        return {"fetchStatus": "no_data", "reason": "No price extracted from API"}

    except Exception as e:
        return {"fetchStatus": "failed", "reason": str(e)}


# ── Market indicators ─────────────────────────────────────────────────────────

def fetch_eurusd(session):
    """EUR/USD from ECB — free, no API key."""
    url = (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
        "?lastNObservations=1&format=jsondata"
    )
    try:
        r = session.get(url, headers={"Accept": "application/json"}, timeout=12)
        r.raise_for_status()
        data   = r.json()
        series = data["dataSets"][0]["series"]
        key    = list(series.keys())[0]
        obs    = series[key]["observations"]
        latest = max(obs.keys(), key=int)
        value  = float(obs[latest][0])
        return {"status": "ok", "value": round(value, 4)}
    except Exception as e:
        return {"status": "failed", "value": None, "reason": str(e)}


def fetch_jet_fuel(session):
    """US Gulf Coast Jet Fuel spot price from FRED (USD/gallon, weekly). Requires FRED_API_KEY."""
    if not FRED_API_KEY:
        return {"status": "no_key", "usdPerGallon": None}
    try:
        r = session.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id":  "WJFUELUSGULF",
                "api_key":    FRED_API_KEY,
                "sort_order": "desc",
                "limit":      1,
                "file_type":  "json",
            },
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        obs  = data["observations"][0]
        if obs["value"] == ".":
            return {"status": "no_data", "usdPerGallon": None}
        return {"status": "ok", "usdPerGallon": round(float(obs["value"]), 4), "asOf": obs["date"]}
    except Exception as e:
        return {"status": "failed", "usdPerGallon": None, "reason": str(e)}


def fetch_airline_inflation(session):
    """Ireland HICP Air Transport index from Eurostat (2015=100, monthly). No API key needed."""
    url = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_midx"
    try:
        r = session.get(
            url,
            params={"lang": "EN", "unit": "I15", "coicop": "CP0733", "geo": "IE"},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data        = r.json()
        times       = data["dimension"]["time"]["category"]["index"]
        sorted_times = sorted(times.items(), key=lambda x: x[1], reverse=True)
        values_data = data["value"]
        for time_label, idx in sorted_times:
            val = values_data.get(str(idx))
            if val is not None:
                return {"status": "ok", "index": round(val, 2), "period": time_label}
        return {"status": "no_data", "index": None}
    except Exception as e:
        return {"status": "failed", "index": None, "reason": str(e)}


def school_holiday_status():
    """Check whether the FLIGHT DATE falls in Irish school holidays."""
    f = FLIGHT_DATE
    if SUMMER_START <= f <= SUMMER_END:
        return {
            "active": True,
            "note":   "Flight is in Irish Summer Holidays (late Jun–Sep) — high family demand, prices elevated",
        }
    if EASTER_START <= f <= EASTER_END:
        return {
            "active": True,
            "note":   "Flight is in Irish Easter Holidays — elevated family demand",
        }
    # Pre-summer proximity warning
    days_to_summer = (SUMMER_START - f).days
    if 0 < days_to_summer <= 21:
        return {
            "active": False,
            "note":   f"Flight is {days_to_summer} days before Irish summer holidays — demand rising, book soon",
        }
    return {"active": False, "note": "Flight not in an Irish school holiday period"}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today    = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days_to_flight = (FLIGHT_DATE - today_dt).days

    # Sweet-spot window: 40–60 days before outbound
    sweet_start = (FLIGHT_DATE.toordinal() - 60)  # ordinal
    sweet_end   = (FLIGHT_DATE.toordinal() - 40)
    today_ord   = today_dt.toordinal()
    in_window   = sweet_start <= today_ord <= sweet_end
    past_window = today_ord > sweet_end

    print(f"\n[{today}] Flight Tracker — DUB→FAO {OUTBOUND_DATE} / FAO→DUB {INBOUND_DATE}")
    print(f"Passengers: {ADULTS} adults + {CHILDREN} children | {days_to_flight}d to flight")
    if in_window:
        print(f"BUY WINDOW: active — {sweet_end - today_ord}d remaining (sweet spot: 40–60d out)")
    elif past_window:
        print("BUY WINDOW: PAST — book immediately, prices likely rising")
    else:
        print(f"BUY WINDOW: starts in {sweet_start - today_ord}d")
    print()

    session = make_session()

    # ── Flight prices ──────────────────────────────────────────────────────────
    print("  Ryanair...              ", end=" ", flush=True)
    ryanair = fetch_ryanair(session)
    if ryanair.get("fetchStatus") == "ok":
        print(f"OK  |  Base €{ryanair['baseFare']}  Bags €{ryanair['baggageFees']}  Total €{ryanair['totalEUR']}")
    else:
        print(f"FAILED — {ryanair.get('reason', '?')}")
    time.sleep(1)

    print("  Ryanair sale check...   ", end=" ", flush=True)
    sale = check_ryanair_sale(session)
    print("SALE ACTIVE: " + sale["details"] if sale["active"] else "No active sales")
    time.sleep(1)

    print("  Skyscanner...           ", end=" ", flush=True)
    skyscanner = fetch_skyscanner(session)
    if skyscanner.get("fetchStatus") == "ok":
        print(f"OK  |  Base €{skyscanner['baseFare']}  Total €{skyscanner['totalEUR']}")
    else:
        print(f"{skyscanner.get('fetchStatus','?').upper()} — {skyscanner.get('reason','?')}")
    time.sleep(1)

    # ── Market indicators ──────────────────────────────────────────────────────
    print("  EUR/USD (ECB)...        ", end=" ", flush=True)
    eurusd = fetch_eurusd(session)
    print(f"OK  {eurusd['value']}" if eurusd.get("status") == "ok" else f"FAILED — {eurusd.get('reason','?')}")
    time.sleep(1)

    print("  Jet fuel (FRED)...      ", end=" ", flush=True)
    jet = fetch_jet_fuel(session)
    if jet.get("status") == "ok":
        print(f"OK  ${jet['usdPerGallon']}/gal  (as of {jet.get('asOf','')})")
    else:
        print(f"{jet.get('status','?').upper()} — {jet.get('reason','Set FRED_API_KEY env var')}")
    time.sleep(1)

    print("  Eurostat airline HICP...", end=" ", flush=True)
    infl = fetch_airline_inflation(session)
    if infl.get("status") == "ok":
        print(f"OK  Index {infl['index']} ({infl.get('period','')})")
    else:
        print(f"{infl.get('status','?').upper()} — {infl.get('reason','?')}")

    # ── Compute EUR/tonne for jet fuel ─────────────────────────────────────────
    jet_eur_tonne = None
    if jet.get("status") == "ok" and jet.get("usdPerGallon") and eurusd.get("status") == "ok":
        usd_tonne     = jet["usdPerGallon"] * 330.22   # gallons per tonne (JetA-1)
        jet_eur_tonne = round(usd_tonne / eurusd["value"], 2)

    holidays = school_holiday_status()

    # ── Build entry ────────────────────────────────────────────────────────────
    entry = {
        "date":       today,
        "ryanair":    ryanair,
        "skyscanner": skyscanner,
        "saleAlert":  sale,
        "indicators": {
            "eurUsd":              eurusd.get("value"),
            "eurUsdStatus":        eurusd.get("status"),
            "jetFuelUsdPerGallon": jet.get("usdPerGallon"),
            "jetFuelEurPerTonne":  jet_eur_tonne,
            "jetFuelStatus":       jet.get("status"),
            "jetFuelAsOf":         jet.get("asOf"),
            "airlineInflIndex":    infl.get("index"),
            "airlineInflPeriod":   infl.get("period"),
            "airlineInflStatus":   infl.get("status"),
            "daysUntilFlight":     days_to_flight,
            "inBuyWindow":         in_window,
            "pastBuyWindow":       past_window,
            "schoolHoliday":       holidays,
        },
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
        print(f"\n  Updated entry for {today}")
    else:
        existing.append(entry)
        print(f"\n  Appended entry for {today}")

    DATA_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved -> {DATA_FILE}\n")


if __name__ == "__main__":
    main()
