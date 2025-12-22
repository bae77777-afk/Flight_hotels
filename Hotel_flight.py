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
) -> List[HotelOption]:
    """Fetch hotels from LiteAPI and return them sorted by price."""

    if not LITEAPI_API_KEY:
        raise RuntimeError("LITEAPI_KEY 환경변수가 설정되지 않았습니다.")

    payload = {
        "occupancies": [{"adults": 2}],
        "sort": [{"field": "price", "direction": "ascending"}],
        "starRating": list(range(min_star, max_star + 1)),
        "currency": currency,
        "guestNationality": nationality,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "timeout": 6,
        "maxRatesPerHotel": 1,
        "boardType": "RO",
        "refundableRatesOnly": False,
        "cityName": city_name,
        "countryCode": country_code,
        "includeHotelData": True,
        "limit": limit,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": LITEAPI_API_KEY,
    }

    resp = requests.post(LITEAPI_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    hotels_raw = data.get("data") or []

    # ✅ (핵심) 호텔 메타데이터가 별도 hotels 배열로 오는 케이스 대응
    hotels_meta = data.get("hotels") or []
    hotel_meta_map = {}
    if isinstance(hotels_meta, list):
        for h in hotels_meta:
            if isinstance(h, dict) and h.get("id"):
                hotel_meta_map[h["id"]] = h

    def extract_name(hotel_info: dict, fallback: dict) -> str:
        return (
            hotel_info.get("name")
            or hotel_info.get("hotelName")
            or fallback.get("hotelName")
            or ""
        )

    def extract_star(hotel_info: dict):
        star = hotel_info.get("starRating")
        if star is None:
            star = hotel_info.get("stars")
        if star is None:
            star = hotel_info.get("rating")
        return star

    def extract_address(hotel_info: dict) -> str:
        addr = hotel_info.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("line1"),
                addr.get("line2"),
                addr.get("city"),
                addr.get("state"),
                addr.get("postalCode"),
                addr.get("country"),
            ]
            return " ".join([p for p in parts if p])
        if isinstance(addr, str):
            return addr
        # 가끔 location 키로 오는 경우 대비
        loc = hotel_info.get("location")
        if isinstance(loc, dict):
            parts = [loc.get("address"), loc.get("city"), loc.get("country")]
            return " ".join([p for p in parts if p])
        return ""

    rows: List[HotelOption] = []

    for hotel_obj in hotels_raw:
        if not isinstance(hotel_obj, dict):
            continue

        hotel_id = hotel_obj.get("hotelId") or hotel_obj.get("id") or ""

        # 1) includeHotelData=True면 여기에 들어올 수도 있음
        hotel_info = hotel_obj.get("hotel") or {}

        # 2) 비어있으면 hotels 메타에서 보강
        if (not hotel_info) and hotel_id and hotel_id in hotel_meta_map:
            hotel_info = hotel_meta_map[hotel_id] or {}

        # ✅ 여기서 name/star/address 확정
        name = extract_name(hotel_info, hotel_obj)
        star = extract_star(hotel_info)
        address = extract_address(hotel_info)

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
                hotel_id=str(hotel_id),
                name=str(name),
                star_rating=star,
                address=str(address),
                total_price=float(total_price),
                currency=curr,
                refundable_tag=refundable_tag,
            )
        )

    rows.sort(key=lambda x: x.total_price)
    for i, r in enumerate(rows, start=1):
        r.rank = i

    return rows
