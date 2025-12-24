from __future__ import annotations

import os
import calendar
from datetime import date, timedelta
from typing import Dict, List

import requests
from flask import Flask, abort, request, render_template_string, render_template

from Hotel_flight import find_cheapest_flight_in_month, search_hotels_for_dates

app = Flask(__name__)

@app.get("/")
def index():
    return render_template("index.html", title="소개")

# --- Secrets / config via environment variables ---
LITEAPI_API_KEY = os.environ.get("LITEAPI_KEY")
ACCESS_KEY = os.environ.get("ACCESS_KEY")  # optional: simple shared secret for friends

LITEAPI_URL = "https://api.liteapi.travel/v3.0/hotels/rates"


def _require_access_key():
    """If ACCESS_KEY is set, require ?key=... on all requests."""
    if not ACCESS_KEY:
        return
    if request.args.get("key") != ACCESS_KEY:
        abort(403)


@app.before_request
def _gatekeeper():
    _require_access_key()


# -----------------------------
# LiteAPI helpers (hotel period/month modes)
# -----------------------------

def fetch_rates(payload: dict) -> dict:
    if not LITEAPI_API_KEY:
        raise RuntimeError("LITEAPI_KEY 환경변수가 설정되지 않았습니다.")

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": LITEAPI_API_KEY,
    }
    resp = requests.post(LITEAPI_URL, json=payload, headers=headers, timeout=60)
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
            return offer.get("amount", float("inf"))

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
    adults: int,
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
        adults=adults
        
        
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
    adults: int,
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
                adults=adults,
            )
        except Exception:
            continue

        if result and result["price"] is not None:
            daily_results.append(result)

    if not daily_results:
        return None, []

    cheapest = min(daily_results, key=lambda r: r["price"])
    return cheapest, daily_results


# -----------------------------
# Template
# -----------------------------




@app.route("/travel", methods=["GET", "POST"])
def travel():
    mode = "flight_hotel_month"

    city = "Sapporo"
    country = "JP"
    adults = 2
    min_stars = 4
    max_stars = 5
    currency = "KRW"
    guest_nat = "KR"

    checkin = "2026-01-01"
    checkout = "2026-01-05"
    top_n = 10
    limit = 50

    year = 2026
    month = 1
    nights = 3

    origin = "ICN"
    dest = "CTS"
    trip = "round-trip"
    seat = "economy"
    flight_adults = 1
    fh_top_n = 10

    period_rows: List[Dict] = []
    monthly_results: List[Dict] = []
    hotel_cheapest = None
    best_flight = None
    fh_hotels = []
    error = None

    combined_total = None

    # 항공 통화 추정 (price_raw에 $/₩ 등이 있으면 간단히 추정)
    flight_currency = None
    if best_flight and getattr(best_flight, "price_raw", None):
        flight_currency = "USD" if "$" in (best_flight.price_raw or "") else None

    
    combined_total = None
    combo_hotel = None

    if best_flight and fh_hotels:
        combo_hotel = fh_hotels[0]
    # 통화가 같을 때만 합산(아래는 예시)
        try:
            combined_total = float(best_flight.price_value) + float(combo_hotel.total_price)
        except Exception:
            combined_total = None


  

    if request.method == "POST":
        form = request.form
        mode = form.get("mode", mode)

        city = form.get("city", city)
        country = form.get("country", country)
        adults = int(form.get("adults", adults))
        min_stars = int(form.get("min_stars", min_stars))
        max_stars = int(form.get("max_stars", max_stars))
        currency = form.get("currency", currency)
        guest_nat = form.get("guest_nat", guest_nat)

        checkin = form.get("checkin", checkin)
        checkout = form.get("checkout", checkout)
        top_n = int(form.get("top_n", top_n))
        limit = int(form.get("limit", limit))

        year = int(form.get("year", year))
        month = int(form.get("month", month))
        nights = int(form.get("nights", nights))

        origin = form.get("origin", origin)
        dest = form.get("dest", dest)
        trip = form.get("trip", trip)
        seat = form.get("seat", seat)
        flight_adults = int(form.get("flight_adults", flight_adults))
        fh_top_n = int(form.get("fh_top_n", fh_top_n))

        try:
            if mode == "hotel_period":
                ci = date.fromisoformat(checkin)
                co = date.fromisoformat(checkout)
                payload = build_payload_for_period(
                    city=city,
                    country=country,
                    checkin=ci,
                    checkout=co,
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
                    adults=adults,
                )

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
                    ci = best_flight.depart_date
                    if best_flight.return_date:
                        co = best_flight.return_date
                    else:
                        co = ci + timedelta(days=nights)

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
                    )
                    if fh_hotels:
                        fh_hotels = fh_hotels[:fh_top_n]

                        combo_hotel = fh_hotels[0]
                        try:
                            combined_total = float(best_flight.price_value) + float(
                                combo_hotel.total_price
                            )
                        except Exception:
                            combined_total = None

        except Exception as e:
            error = str(e)

    return render_template(
    "travel.html",
    title="여행툴",
    mode=mode,
    city=city,
    country=country,
    adults=adults,
    min_stars=min_stars,
    max_stars=max_stars,
    currency=currency,
    guest_nat=guest_nat,
    checkin=checkin,
    checkout=checkout,
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
    error=error,
    period_rows=period_rows,
    monthly_results=monthly_results,
    hotel_cheapest=hotel_cheapest,
    best_flight=best_flight,
    fh_hotels=fh_hotels,
    combined_total=combined_total,
    combo_hotel=combo_hotel,
)



if __name__ == "__main__":
    # Render 등 배포환경에서는 gunicorn이 실행함
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
