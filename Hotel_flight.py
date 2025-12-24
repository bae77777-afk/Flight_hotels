from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from calendar import monthrange
from typing import Optional, List

import requests
from fast_flights import FlightData, Passengers, Result, get_flights


# ==========================
#  Data structures
# ==========================

@dataclass
class FlightOption:
    depart_date: date
    return_date: Optional[date]
    price_value: float
    price_raw: str
    airline: str


@dataclass
class HotelOption:
    rank: int
    hotel_id: str
    name: str
    star_rating: Optional[int]
    address: str
    total_price: float
    currency: str
    refundable_tag: str


# ==========================
#  LiteAPI settings (via env)
# ==========================

LITEAPI_BASE_URL = "https://api.liteapi.travel/v3.0"
LITEAPI_URL = f"{LITEAPI_BASE_URL}/hotels/rates"
LITEAPI_API_KEY = os.environ.get("LITEAPI_KEY")


def parse_price_to_float(price) -> float:
    """Convert fast_flights price (string/number) to float."""
    if price is None:
        return float("inf")
    if isinstance(price, (int, float)):
        return float(price)

    s = str(price)
    digits = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    if not digits:
        return float("inf")
    try:
        return float(digits)
    except ValueError:
        return float("inf")


# ==========================
#  Flights (fast_flights)
# ==========================


def search_flights_for_date(
    depart: date,
    origin: str,
    dest: str,
    trip: str = "one-way",
    return_date: Optional[date] = None,
    adults: int = 1,
    seat: str = "economy",
) -> Optional[FlightOption]:
    """Return the cheapest flight for the given date(s) using fast_flights."""

    date_str = depart.isoformat()
    return_date_str = return_date.isoformat() if return_date else None

    if trip == "round-trip" and return_date_str:
        flight_data = [
            FlightData(date=date_str, from_airport=origin, to_airport=dest),
            FlightData(date=return_date_str, from_airport=dest, to_airport=origin),
        ]
    else:
        flight_data = [FlightData(date=date_str, from_airport=origin, to_airport=dest)]

    passengers = Passengers(
        adults=adults,
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
    )

    try:
        result: Result = get_flights(
            flight_data=flight_data,
            trip="round-trip" if trip == "round-trip" else "one-way",
            seat=seat,
            passengers=passengers,
            fetch_mode="fallback",
            
        )
    except Exception:
        return None

    flights = getattr(result, "flights", None)
    if not flights:
        return None

    cheapest = min(flights, key=lambda f: parse_price_to_float(getattr(f, "price", None)))
    price_raw = getattr(cheapest, "price", "")
    airline = getattr(cheapest, "name", "") or getattr(cheapest, "airline", "")

    return FlightOption(
        depart_date=depart,
        return_date=return_date,
        price_value=parse_price_to_float(price_raw),
        price_raw=str(price_raw),
        airline=airline or "N/A",
    )


def find_cheapest_flight_in_month(
    year: int,
    month: int,
    origin: str,
    dest: str,
    trip: str,
    stay_nights: int,
    adults: int,
    seat: str,
) -> Optional[FlightOption]:
    """Find the cheapest option within the month for the given route."""

    days_in_month = monthrange(year, month)[1]
    best: Optional[FlightOption] = None

    for day in range(1, days_in_month + 1):
        depart = date(year, month, day)
        return_d = depart + timedelta(days=stay_nights) if trip == "round-trip" else None

        option = search_flights_for_date(
            depart=depart,
            origin=origin,
            dest=dest,
            trip=trip,
            return_date=return_d,
            adults=adults,
            seat=seat,
        )
        if option is None:
            continue

        if best is None or option.price_value < best.price_value:
            best = option

    return best


# ==========================
#  Hotels (LiteAPI)
# ==========================


def search_hotels_for_dates(
    checkin: date,
    checkout: date,
    city_name: str,
    country_code: str,
    min_star: int = 4,
    max_star: int = 5,
    limit: int = 100,
    currency: str = "KRW",
    nationality: str = "KR",
    adults: int = 2,   # ✅ 추가: 성인 수 반영
) -> List[HotelOption]:
    """Fetch hotels from LiteAPI and return them sorted by price."""

    if not LITEAPI_API_KEY:
        raise RuntimeError("LITEAPI_KEY 환경변수가 설정되지 않았습니다.")

    if min_star > max_star:
        min_star, max_star = max_star, min_star

    payload = {
        "occupancies": [{"adults": int(adults)}],  # ✅ 고정 2 -> 입력값
        "sort": [{"field": "price", "direction": "ascending"}],
        "starRating": list(range(int(min_star), int(max_star) + 1)),
        "currency": currency,
        "guestNationality": nationality,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "maxRatesPerHotel": 1,
        "boardType": "RO",
        "refundableRatesOnly": False,
        "cityName": city_name,
        "countryCode": country_code,
        "includeHotelData": True,
        "limit": int(limit),
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": LITEAPI_API_KEY,
    }

    try:
        # connect 10초, read 60초
        resp = requests.post(LITEAPI_URL, json=payload, headers=headers, timeout=(10, 60))
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return []
    except requests.exceptions.RequestException:
        return []
    except ValueError:
        # JSON 파싱 실패
        return []

    hotels_raw = data.get("data") or []

    # 호텔 메타 보강
    hotels_meta = data.get("hotels") or []
    hotel_meta_map = {h.get("id"): h for h in hotels_meta if h.get("id")}

    rows: List[HotelOption] = []

    for hotel_obj in hotels_raw:
        hotel_id = hotel_obj.get("hotelId") or ""

        hotel_info = hotel_obj.get("hotel") or {}
        if (not hotel_info) and hotel_id in hotel_meta_map:
            hotel_info = hotel_meta_map[hotel_id] or {}

        name = (
            hotel_info.get("name")
            or hotel_info.get("hotelName")
            or hotel_obj.get("hotelName")
            or ""
        )

        star = hotel_info.get("starRating")
        if star is None:
            star = hotel_info.get("rating")

        address = ""
        addr = hotel_info.get("address")
        if isinstance(addr, dict):
            address = addr.get("line1") or addr.get("city") or ""
        elif isinstance(addr, str):
            address = addr

        room_types = hotel_obj.get("roomTypes") or []
        if not room_types:
            continue

        def get_offer_amount(rt: dict) -> float:
            offer = rt.get("offerRetailRate") or {}
            return offer.get("amount", float("inf"))

        best_room = min(room_types, key=get_offer_amount)
        offer = best_room.get("offerRetailRate") or {}
        total_price = offer.get("amount")
        curr = offer.get("currency", currency)

        first_rate = (best_room.get("rates") or [{}])[0]
        refundable_tag = (first_rate.get("cancellationPolicies") or {}).get("refundableTag", "")

        if total_price is None:
            continue

        rows.append(
            HotelOption(
                rank=0,
                hotel_id=hotel_id,
                name=name,
                star_rating=star,
                address=address,
                total_price=float(total_price),
                currency=curr,
                refundable_tag=refundable_tag,
            )
        )

    rows.sort(key=lambda x: x.total_price)
    for i, r in enumerate(rows, start=1):
        r.rank = i

    return rows
