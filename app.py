from __future__ import annotations

import os
import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional
from types import SimpleNamespace

import requests
from flask import Flask, abort, request, render_template

from Hotel_flight import find_cheapest_flight_in_month, search_hotels_for_dates

app = Flask(__name__)

# --- Secrets / config via environment variables ---
LITEAPI_API_KEY = os.environ.get("LITEAPI_KEY")
ACCESS_KEY = os.environ.get("ACCESS_KEY")  # optional: simple shared secret for friends

LITEAPI_URL = "https://api.liteapi.travel/v3.0/hotels/rates"
USD_TO_KRW = 1450  # 대충 환산(원하면 나중에 환율 API로 바꾸면 됨)


# -----------------------------
# Access gate (travel only)
# -----------------------------
def _require_access_key_for_travel():
    """ACCESS_KEY가 설정돼 있으면 /travel 접근을 key로 제한."""
    if not ACCESS_KEY:
        return
    if request.endpoint != "travel":
        return

    provided = (
        request.args.get("key")
        or request.form.get("key")
        or request.headers.get("X-Access-Key")
    )
    if provided != ACCESS_KEY:
        abort(403)


@app.before_request
def _gatekeeper():
    _require_access_key_for_travel()


# -----------------------------
# Intro
# -----------------------------
@app.get("/")
def index():
    return render_template("index.html", title="소개")


# -----------------------------
# LiteAPI helpers (hotel period/month)
# -----------------------------
def fetch_rates(payload: dict) -> dict:
    if not LITEAPI_API_KEY:
        raise RuntimeError("LITEAPI_KEY 환경변수가 설정되지 않았습니다.")

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": LITEAPI_API_KEY,
    }
    resp = requests.post(LITEAPI_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()


def build_payload_for_period(
    city: str,
    country: str,
    checkin: date,
    checkout: date,
    min_stars: int,
    max_stars: int,
    adults: int,
    guest_nationality: str,
    currency: str,
    limit: int,
) -> dict:
    star_list = list(range(min_stars, max_stars + 1))
    return {
        "occupancies": [{"adults": adults}],
        "sort": [{"field": "price", "direction": "ascending"}],
        "starRating": star_list,
        "currency": currency,
        "guestNationality": guest_nationality,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "timeout": 6,
        "maxRatesPerHotel": 1,
        "boardType": "RO",
        "refundableRatesOnly": False,
        "cityName": city,
        "countryCode": country,
        "includeHotelData": True,
        "limit": limit,
    }


def build_rows_from_rates(resp_json: dict, top_n: int = 10) -> List[Dict]:
    data = resp_json.get("data", [])
    hotels_meta = resp_json.get("hotels", [])
    hotel_meta_map = {h.get("id"): h for h in hotels_meta}

    rows: List[Dict] = []

    for idx, item in enumerate(data[:top_n], start=1):
        hotel_id = item.get("hotelId")
        if not hotel_id:
            continue

        meta = hotel_meta_map.get(hotel_id, {})
        hotel_name = meta.get("name", "")
        address = meta.get("address", "")
        rating = meta.get("rating", "")

        room_types = item.get("roomTypes") or []
        if not room_types:
            continue

        def get_offer_amount(rt: dict) -> float:
            offer = rt.get("offerRetailRate") or {}
            amount = offer.get("amount", None)
            try:
                return float(amount)
            except Exception:
                return float("inf")

        best_room = min(room_types, key=get_offer_amount)
        offer = best_room.get("offerRetailRate") or {}
        total_price = offer.get("amount")
        curr = offer.get("currency")

        first_rate = (best_room.get("rates") or [{}])[0]
        refundable_tag = (first_rate.get("cancellationPolicies") or {}).get(
            "refundableTag", ""
        )

        rows.append(
            {
                "no": idx,
                "name": hotel_name,
                "hotel_id": hotel_id,
                "address": address,
                "rating": rating,
                "price": total_price,
                "currency": curr,
                "refundable": refundable_tag,
            }
        )

    return rows


def get_min_price_for_date_via_helper(
    city: str,
    country: str,
    checkin: date,
    nights: int,
    min_stars: int,
    max_stars: int,
    currency: str,
    nationality: str,
    limit: int,
):
    checkout = checkin + timedelta(days=nights)
    hotels = search_hotels_for_dates(
        checkin=checkin,
        checkout=checkout,
        city_name=city,
        country_code=country,
        min_star=min_stars,
        max_star=max_stars,
        limit=limit,
        currency=currency,
        nationality=nationality,
    )
    if not hotels:
        return None

    best = hotels[0]
    return {
        "checkin": checkin,
        "checkout": checkout,
        "price": best.total_price,
        "currency": best.currency,
        "hotelName": best.name,
        "hotelId": best.hotel_id,
    }


def find_cheapest_hotel_in_month(
    city: str,
    country: str,
    year: int,
    month: int,
    nights: int,
    min_stars: int,
    max_stars: int,
    currency: str,
    nationality: str,
    limit: int,
):
    last_day = calendar.monthrange(year, month)[1]
    daily_results: List[Dict] = []

    for day in range(1, last_day + 1):
        checkin = date(year, month, day)
        try:
            result = get_min_price_for_date_via_helper(
                city=city,
                country=country,
                checkin=checkin,
                nights=nights,
                min_stars=min_stars,
                max_stars=max_stars,
                currency=currency,
                nationality=nationality,
                limit=limit,
            )
        except Exception:
            continue

        if result and result["price"] is not None:
            daily_results.append(result)

    if not daily_results:
        return None, []

    cheapest = min(daily_results, key=lambda r: float(r["price"]))
    return cheapest, daily_results


# -----------------------------
# Flight helper (period mode)
# -----------------------------
def _parse_price_value(price_raw: str) -> Optional[float]:
    if not price_raw:
        return None
    cleaned = "".join(ch for ch in str(price_raw) if ch.isdigit() or ch == ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def find_cheapest_flight_for_dates(
    depart: date,
    ret: Optional[date],
    origin: str,
    dest: str,
    trip: str,
    adults: int,
    seat: str,
):
    """
    fast_flights로 해당 날짜/노선 최저가 항공 1개 반환.
    (find_cheapest_flight_in_month() 결과처럼 SimpleNamespace로 맞춰줌)
    """
    try:
        from fast_flights import FlightData, Passengers, get_flights  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "fast-flights 라이브러리를 불러오지 못했습니다. requirements.txt에 fast-flights가 필요합니다."
        ) from e

    flight_data = [FlightData(date=depart.isoformat(), from_airport=origin, to_airport=dest)]
    if trip == "round-trip":
        if not ret:
            raise ValueError("왕복(trip=round-trip)에는 귀국일(ret)이 필요합니다.")
        flight_data.append(FlightData(date=ret.isoformat(), from_airport=dest, to_airport=origin))

    result = get_flights(
        flight_data=flight_data,
        trip=trip,
        seat=seat,
        passengers=Passengers(adults=adults),
        fetch_mode="fallback",
    )

    flights = getattr(result, "flights", None) or []
    if not flights:
        return None

    def _price_num(f):
        val = _parse_price_value(getattr(f, "price", "") or "")
        return val if val is not None else float("inf")

    best = min(flights, key=_price_num)
    price_raw = getattr(best, "price", "") or ""
    price_value = _parse_price_value(price_raw)
    airline = getattr(best, "name", "") or getattr(best, "airline", "") or ""

    return SimpleNamespace(
        depart_date=depart,
        return_date=ret if trip == "round-trip" else None,
        airline=airline,
        price_raw=price_raw,
        price_value=price_value,
        currency="USD" if "$" in price_raw else "",
        _raw_flight=best,
    )


# -----------------------------
# Travel page
# -----------------------------
@app.route("/travel", methods=["GET", "POST"])
def travel():
    # 기본값
    mode = request.form.get("mode") or "hotel_period"

    city = request.form.get("city") or "Tokyo"
    country = request.form.get("country") or "JP"

    def _int(name: str, default: int) -> int:
        try:
            return int(request.form.get(name, default))
        except Exception:
            return default

    adults = _int("adults", 2)
    min_stars = _int("min_stars", 4)
    max_stars = _int("max_stars", 5)
    currency = request.form.get("currency") or "KRW"
    guest_nat = request.form.get("guest_nat") or "KR"

    checkin_s = request.form.get("checkin") or date.today().isoformat()
    checkout_s = request.form.get("checkout") or (date.today() + timedelta(days=1)).isoformat()
    top_n = _int("top_n", 10)
    limit = _int("limit", 50)

    year = _int("year", date.today().year)
    month = _int("month", date.today().month)
    nights = _int("nights", 3)

    origin = (request.form.get("origin") or "ICN").upper()
    dest = (request.form.get("dest") or "NRT").upper()
    trip = request.form.get("trip") or "round-trip"
    seat = request.form.get("seat") or "economy"
    flight_adults = _int("flight_adults", 1)
    fh_top_n = _int("fh_top_n", 10)

    # 결과 변수들
    error = None
    period_rows = []
    monthly_results = []
    hotel_cheapest = None

    best_flight = None
    fh_hotels = []
    combo_hotel = None
    combined_total = None
    flight_price_krw = None

    if request.method == "POST":
        try:
            # 날짜 파싱
            checkin = date.fromisoformat(checkin_s)
            checkout = date.fromisoformat(checkout_s)

            if mode == "hotel_period":
                payload = build_payload_for_period(
                    city=city,
                    country=country,
                    checkin=checkin,
                    checkout=checkout,
                    min_stars=min_stars,
                    max_stars=max_stars,
                    adults=adults,
                    guest_nationality=guest_nat,
                    currency=currency,
                    limit=limit,
                )
                resp_json = fetch_rates(payload)
                period_rows = build_rows_from_rates(resp_json, top_n=top_n)

            elif mode == "hotel_month":
                hotel_cheapest, monthly_results = find_cheapest_hotel_in_month(
                    city=city,
                    country=country,
                    year=year,
                    month=month,
                    nights=nights,
                    min_stars=min_stars,
                    max_stars=max_stars,
                    currency=currency,
                    nationality=guest_nat,
                    limit=limit,
                )

            elif mode == "flight_hotel_period":
                # 입력한 checkin/checkout을 항공 날짜로 사용
                depart = checkin
                ret = checkout if trip == "round-trip" else None

                best_flight = find_cheapest_flight_for_dates(
                    depart=depart,
                    ret=ret,
                    origin=origin,
                    dest=dest,
                    trip=trip,
                    adults=flight_adults,
                    seat=seat,
                )

                # 호텔은 여행기간 그대로
                fh_hotels = search_hotels_for_dates(
                    checkin=checkin,
                    checkout=checkout,
                    city_name=city,
                    country_code=country,
                    min_star=min_stars,
                    max_star=max_stars,
                    limit=limit,
                    currency=currency,
                    nationality=guest_nat,
                ) or []
                fh_hotels = fh_hotels[:fh_top_n]

                if best_flight:
                    raw = getattr(best_flight, "price_raw", "") or ""
                    if "$" in raw or getattr(best_flight, "currency", "") == "USD":
                        try:
                            flight_price_krw = int(float(best_flight.price_value) * USD_TO_KRW)
                        except Exception:
                            flight_price_krw = None
                    else:
                        try:
                            flight_price_krw = int(float(best_flight.price_value))
                        except Exception:
                            flight_price_krw = None

                if fh_hotels:
                    combo_hotel = fh_hotels[0]
                    if flight_price_krw is not None:
                        try:
                            combined_total = int(flight_price_krw) + int(combo_hotel.total_price)
                        except Exception:
                            combined_total = None

            elif mode == "flight_hotel_month":
                best_flight = find_cheapest_flight_in_month(
                    year=year,
                    month=month,
                    origin=origin,
                    dest=dest,
                    trip=trip,
                    stay_nights=nights,
                    adults=flight_adults,
                    seat=seat,
                )

                if best_flight:
                    raw = getattr(best_flight, "price_raw", "") or ""
                    if "$" in raw or getattr(best_flight, "currency", "") == "USD":
                        try:
                            flight_price_krw = int(float(best_flight.price_value) * USD_TO_KRW)
                        except Exception:
                            flight_price_krw = None
                    else:
                        try:
                            flight_price_krw = int(float(best_flight.price_value))
                        except Exception:
                            flight_price_krw = None

                    ci = best_flight.depart_date
                    co = best_flight.return_date if best_flight.return_date else (ci + timedelta(days=nights))

                    fh_hotels = search_hotels_for_dates(
                        checkin=ci,
                        checkout=co,
                        city_name=city,
                        country_code=country,
                        min_star=min_stars,
                        max_star=max_stars,
                        limit=limit,
                        currency=currency,
                        nationality=guest_nat,
                    ) or []
                    fh_hotels = fh_hotels[:fh_top_n]

                    if fh_hotels:
                        combo_hotel = fh_hotels[0]
                        if flight_price_krw is not None:
                            try:
                                combined_total = int(flight_price_krw) + int(combo_hotel.total_price)
                            except Exception:
                                combined_total = None

        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    return render_template(
        "travel.html",
        # mode / inputs
        mode=mode,
        city=city,
        country=country,
        adults=adults,
        min_stars=min_stars,
        max_stars=max_stars,
        currency=currency,
        guest_nat=guest_nat,
        checkin=checkin_s,
        checkout=checkout_s,
        top_n=top_n,
        limit=limit,
        year=year,
        month=month,
        nights=nights,
        origin=origin,
        dest=dest,
        trip=trip,
        seat=seat,
        flight_adults=flight_adults,
        fh_top_n=fh_top_n,
        # results
        period_rows=period_rows,
        monthly_results=monthly_results,
        hotel_cheapest=hotel_cheapest,
        best_flight=best_flight,
        fh_hotels=fh_hotels,
        combo_hotel=combo_hotel,
        flight_price_krw=flight_price_krw,
        combined_total=combined_total,
        error=error,
        # access key hint
        has_access_key=bool(ACCESS_KEY),
        current_key=request.args.get("key") or request.form.get("key") or "",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)

