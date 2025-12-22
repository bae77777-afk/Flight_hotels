from __future__ import annotations

import os
import calendar
from datetime import date, timedelta
from typing import Dict, List

import requests
from flask import Flask, abort, request, render_template_string

from Hotel_flight import find_cheapest_flight_in_month, search_hotels_for_dates

app = Flask(__name__)

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
    resp = requests.post(LITEAPI_URL, json=payload, headers=headers, timeout=20)
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

    cheapest = min(daily_results, key=lambda r: r["price"])
    return cheapest, daily_results


# -----------------------------
# Template
# -----------------------------

TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>항공 + 호텔 최저가 툴</title>
  <style>
    body { font-family: sans-serif; max-width: 1100px; margin: 20px auto; }
    fieldset { margin-bottom: 1rem; }
    table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
    th, td { border: 1px solid #ccc; padding: 4px 6px; font-size: 0.9rem; }
    th { background: #f0f0f0; }
    .mode-select { display: flex; gap: 1rem; margin-bottom: .5rem; }
    .section-title { margin-top: 1.5rem; }
  </style>
</head>
<body>
  <h1>항공 + 호텔 최저가 웹툴</h1>
  <form method="post">
    <fieldset>
      <legend>검색 모드</legend>
      <div class="mode-select">
        <label><input type="radio" name="mode" value="hotel_period" {{ 'checked' if mode == 'hotel_period' else '' }}> 호텔 최저가 (여행기간)</label>
        <label><input type="radio" name="mode" value="hotel_month" {{ 'checked' if mode == 'hotel_month' else '' }}> 호텔 최저가 (한달)</label>
        <label><input type="radio" name="mode" value="flight_hotel_month" {{ 'checked' if mode == 'flight_hotel_month' else '' }}> 항공 + 호텔 (한달 최저가)</label>
      </div>
    </fieldset>

    <fieldset>
      <legend>호텔 공통 설정</legend>
      <label>도시 (cityName): <input name="city" value="{{ city }}"></label>
      <label>국가코드 (countryCode): <input name="country" value="{{ country }}" size="4"></label>
      <br><br>
      <label>성인 인원수: <input name="adults" value="{{ adults }}" size="3"></label>
      <label>최소 성급: <input name="min_stars" value="{{ min_stars }}" size="3"></label>
      <label>최대 성급: <input name="max_stars" value="{{ max_stars }}" size="3"></label>
      <label>통화: <input name="currency" value="{{ currency }}" size="5"></label>
      <label>국적: <input name="guest_nat" value="{{ guest_nat }}" size="4"></label>
    </fieldset>

    <fieldset>
      <legend>호텔 최저가 (여행기간)</legend>
      <label>체크인 (YYYY-MM-DD): <input name="checkin" value="{{ checkin }}"></label>
      <label>체크아웃 (YYYY-MM-DD): <input name="checkout" value="{{ checkout }}"></label>
      <label>Top N: <input name="top_n" value="{{ top_n }}" size="3"></label>
      <label>Limit: <input name="limit" value="{{ limit }}" size="4"></label>
    </fieldset>

    <fieldset>
      <legend>호텔 / 항공 한달 검색 공통</legend>
      <label>기준 연도: <input name="year" value="{{ year }}" size="5"></label>
      <label>기준 월: <input name="month" value="{{ month }}" size="3"></label>
      <label>숙박일수(박): <input name="nights" value="{{ nights }}" size="3"></label>
    </fieldset>

    <fieldset>
      <legend>항공 설정 (항공 + 호텔 모드에서 사용)</legend>
      <label>출발 공항 (IATA): <input name="origin" value="{{ origin }}" size="5"></label>
      <label>도착 공항 (IATA): <input name="dest" value="{{ dest }}" size="5"></label>
      <label>여정 타입:
        <select name="trip">
          <option value="round-trip" {% if trip == 'round-trip' %}selected{% endif %}>왕복</option>
          <option value="one-way" {% if trip == 'one-way' %}selected{% endif %}>편도</option>
        </select>
      </label>
      <label>좌석 등급:
        <select name="seat">
          <option value="economy" {% if seat == 'economy' %}selected{% endif %}>Economy</option>
          <option value="premium_economy" {% if seat == 'premium_economy' %}selected{% endif %}>Premium Economy</option>
          <option value="business" {% if seat == 'business' %}selected{% endif %}>Business</option>
          <option value="first" {% if seat == 'first' %}selected{% endif %}>First</option>
        </select>
      </label>
      <label>항공 검색 인원수: <input name="flight_adults" value="{{ flight_adults }}" size="3"></label>
      <label>호텔 TOP N (항공+호텔 모드): <input name="fh_top_n" value="{{ fh_top_n }}" size="3"></label>
    </fieldset>

    <button type="submit">검색하기</button>
  </form>

  {% if error %}
    <p style="color:red;">에러: {{ error }}</p>
  {% endif %}

  {% if mode == 'hotel_period' and period_rows %}
    <h2 class="section-title">호텔 최저가 (여행기간) 결과</h2>
    <table>
      <tr>
        <th>No</th><th>호텔 이름</th><th>성급</th><th>총액</th><th>통화</th><th>환불여부</th><th>주소</th>
      </tr>
      {% for r in period_rows %}
      <tr>
        <td>{{ r.no }}</td>
        <td>{{ r.name }}</td>
        <td>{{ r.rating }}</td>
        <td style="text-align:right">{{ r.price }}</td>
        <td>{{ r.currency }}</td>
        <td>{{ r.refundable }}</td>
        <td>{{ r.address }}</td>
      </tr>
      {% endfor %}
    </table>
  {% endif %}

  {% if mode == 'hotel_month' and monthly_results %}
    <h2 class="section-title">호텔 최저가 (한달 스캔) 결과 – {{ year }}-{{ '%02d'|format(month) }}</h2>
    {% if hotel_cheapest %}
      <p><strong>이 달 호텔 최저가</strong>:
        {{ hotel_cheapest.checkin }} ~ {{ hotel_cheapest.checkout }},
        {{ hotel_cheapest.hotelName }} - {{ hotel_cheapest.price }} {{ hotel_cheapest.currency }}
      </p>
    {% endif %}
    <table>
      <tr>
        <th>No</th><th>Checkin</th><th>Checkout</th><th>호텔</th><th>Price</th><th>통화</th>
      </tr>
      {% for r in monthly_results %}
      <tr>
        <td>{{ loop.index }}</td>
        <td>{{ r.checkin }}</td>
        <td>{{ r.checkout }}</td>
        <td>{{ r.hotelName }}</td>
        <td style="text-align:right">{{ r.price }}</td>
        <td>{{ r.currency }}</td>
      </tr>
      {% endfor %}
    </table>
  {% endif %}

  {% if mode == 'flight_hotel_month' and best_flight %}
    <h2 class="section-title">한달 기준 항공 + 호텔 최저가</h2>
    <h3>① 이 달 최저가 항공 일정</h3>
    <ul>
      <li>출발 공항: {{ origin }} → 도착 공항: {{ dest }}</li>
      <li>출발일: {{ best_flight.depart_date }}</li>
      {% if best_flight.return_date %}
        <li>귀국일: {{ best_flight.return_date }}</li>
      {% else %}
        <li>편도 (체류 {{ nights }}박 기준 호텔 검색)</li>
      {% endif %}
      <li>항공사: {{ best_flight.airline }}</li>
      <li>가격: {{ best_flight.price_raw }} (추출값: {{ best_flight.price_value|round(0) }})</li>
    </ul>

    {% if fh_hotels %}
      <h3>② 해당 일정 기준 호텔 최저가 TOP {{ fh_hotels|length }}</h3>
      <table>
        <tr>
          <th>Rank</th><th>호텔 이름</th><th>성급</th><th>총액</th><th>통화</th><th>환불여부</th><th>주소</th>
        </tr>
        {% for h in fh_hotels %}
        <tr>
          <td>{{ h.rank }}</td>
          <td>{{ h.name }}</td>
          <td>{{ h.star_rating }}</td>
          <td style="text-align:right">{{ h.total_price }}</td>
          <td>{{ h.currency }}</td>
          <td>{{ h.refundable_tag }}</td>
          <td>{{ h.address }}</td>
        </tr>
        {% endfor %}
      </table>

      {% if combined_total and combo_hotel %}
        <h3>③ 항공 + 호텔 합산 최저가</h3>
        <p>
          항공 (약 {{ best_flight.price_value|round(0) }}) +
          호텔 “{{ combo_hotel.name }}” ({{ combo_hotel.total_price }}) =
          <strong>{{ combined_total|round(0) }}</strong>
          {{ combo_hotel.currency }} (동일 통화 기준 추정)
        </p>
      {% endif %}

    {% else %}
      <p>해당 일정에 대한 호텔 검색 결과가 없습니다.</p>
    {% endif %}
  {% endif %}

</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
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
    combo_hotel = None

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

    return render_template_string(
        TEMPLATE,
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
        period_rows=period_rows,
        monthly_results=monthly_results,
        hotel_cheapest=hotel_cheapest,
        best_flight=best_flight,
        fh_hotels=fh_hotels,
        error=error,
        combined_total=combined_total,
        combo_hotel=combo_hotel,
    )


if __name__ == "__main__":
    # Render 등 배포환경에서는 gunicorn이 실행함
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
