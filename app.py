from __future__ import annotations

import os
import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional
from types import SimpleNamespace

import requests
from flask import Flask, abort, request, render_template_string

from Hotel_flight import find_cheapest_flight_in_month, search_hotels_for_dates

USD_TO_KRW = 1480


def _parse_price_value(price_raw: str) -> Optional[float]:
    """Extract numeric price from strings like '₩123,456' or '$123.45'."""
    if not price_raw:
        return None
    # keep digits + dot only
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
    """Return a SimpleNamespace similar to find_cheapest_flight_in_month() output."""
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

    # fetch_mode='fallback'이 가장 무난 (차단/동의 화면 등 대응)
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

    # name 필드가 보통 항공사/상품명에 해당
    airline = getattr(best, "name", "") or getattr(best, "airline", "") or ""

    return SimpleNamespace(
        depart_date=depart,
        return_date=ret if trip == "round-trip" else None,
        airline=airline,
        price_raw=price_raw,
        price_value=price_value,
        currency="USD" if "$" in price_raw else "",  # 단순 추정 (기존 코드와 호환)
        _raw_flight=best,
    )


app = Flask(__name__)

# --- Secrets / config via environment variables ---
LITEAPI_API_KEY = os.environ.get("LITEAPI_KEY")
ACCESS_KEY = os.environ.get("ACCESS_KEY")  # optional: simple shared secret for friends

LITEAPI_URL = "https://api.liteapi.travel/v3.0/hotels/rates"


def _require_access_key():
    if not ACCESS_KEY:
        return

    # ✅ 첫 화면(GET)은 허용
    if request.method == "GET":
        return

    # POST만 보호 (query/form/header 모두 허용)
    provided = (
        request.args.get("key")
        or request.form.get("key")
        or request.headers.get("X-Access-Key")
    )
    if provided != ACCESS_KEY:
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
            # LiteAPI 응답에서 amount가 None/문자열로 올 수 있어 방어
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
      .hidden { display: none; }
  </style>
</head>
<body>
  <h1>항공 + 호텔 최저가 웹툴</h1>
  <form method="post">
    {# ACCESS_KEY를 쓰는 경우, GET 쿼리스트링의 key를 POST에도 유지 #}
    {% if current_key %}
      <input type="hidden" name="key" value="{{ current_key }}">
    {% endif %}
    <fieldset>
      <legend>검색 모드</legend>
      <div class="mode-select">
        <label><input type="radio" name="mode" value="hotel_period" {{ 'checked' if mode == 'hotel_period' else '' }}> 호텔 최저가 (여행기간)</label>
        <label><input type="radio" name="mode" value="hotel_month" {{ 'checked' if mode == 'hotel_month' else '' }}> 호텔 최저가 (한달)</label>
        <label><input type="radio" name="mode" value="flight_hotel_month" {{ 'checked' if mode == 'flight_hotel_month' else '' }}> 항공 + 호텔 (한달 최저가)</label>
        <label><input type="radio" name="mode" value="flight_hotel_period" {{ 'checked' if mode == 'flight_hotel_period' else '' }}> 항공 + 호텔 (여행기간 최저가)</label>
      </div>
    </fieldset>

    <fieldset id="fs_hotel_common">
      <legend>호텔 공통 설정</legend>
      <label>도시 (cityName): <input name="city" value="{{ city }}"></label>
      <label>국가코드 (countryCode): <input name="country" value="{{ country }}" size="4"></label>
      <br><br>
      <label>성인 인원수: <input name="adults" value="{{ adults }}" size="3"></label>
      <label>최소 성급: <input name="min_stars" value="{{ min_stars }}" size="3"></label>
      <label>최대 성급: <input name="max_stars" value="{{ max_stars }}" size="3"></label>
      <label>통화: <input name="currency" value="{{ currency }}" size="5"></label>
      <label>국적: <input name="guest_nat" value="{{ guest_nat }}" size="4"></label>
      <label>검색 Limit(요청 호텔 수): <input name="limit" value="{{ limit }}" size="4"></label>
    </fieldset>

        <fieldset id="fs_hotel_period">
      <legend>호텔 최저가 (여행기간)</legend>
      <label>체크인 (YYYY-MM-DD): <input name="checkin" value="{{ checkin }}"></label>
      <label>체크아웃 (YYYY-MM-DD): <input name="checkout" value="{{ checkout }}"></label>
      <label>Top N: <input name="top_n" value="{{ top_n }}" size="3"></label>
    </fieldset>

    <fieldset id="fs_trip_period">
      <legend>항공 + 호텔 (여행기간)</legend>
      <label>출국일(체크인 기준) (YYYY-MM-DD): <input name="checkin" value="{{ checkin }}"></label>
      <label>귀국일(체크아웃 기준) (YYYY-MM-DD): <input name="checkout" value="{{ checkout }}"></label>
    </fieldset>

    <fieldset id="fs_month_common">
      <legend>호텔 / 항공 한달 검색 공통</legend>
      <label>기준 연도: <input name="year" value="{{ year }}" size="5"></label>
      <label>기준 월: <input name="month" value="{{ month }}" size="3"></label>
      <label>숙박일수(박): <input name="nights" value="{{ nights }}" size="3"></label>
    </fieldset>

    <fieldset id="fs_flight">
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
      {% if flight_price_krw %}
        <li>KRW 환산(대략): {{ flight_price_krw }}</li>
      {% endif %}
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
          항공 (KRW 환산 약 {{ flight_price_krw|default(best_flight.price_value|round(0)) }}) +
          호텔 “{{ combo_hotel.name }}” ({{ combo_hotel.total_price }}) =
          <strong>{{ combined_total|round(0) }}</strong>
          KRW
        </p>
      {% endif %}

    {% else %}
      <p>해당 일정에 대한 호텔 검색 결과가 없습니다.</p>
    {% endif %}
  {% endif %}

  {% if mode == 'flight_hotel_period' and best_flight %}
    <h2 class="section-title">기간 기준 항공 + 호텔 최저가</h2>
    <h3>① 선택한 기간 기준 항공</h3>
    <ul>
      <li>출발 공항: {{ origin }} → 도착 공항: {{ dest }}</li>
      <li>출발일(체크인): {{ best_flight.depart_date }}</li>
      {% if best_flight.return_date %}
        <li>귀국일(체크아웃): {{ best_flight.return_date }}</li>
      {% else %}
        <li>편도 (귀국 항공 없음)</li>
      {% endif %}
      <li>항공사/상품: {{ best_flight.airline }}</li>
      <li>가격: {{ best_flight.price_raw }}
        {% if best_flight.price_value is not none %}
          (추출값: {{ best_flight.price_value|round(0) }})
        {% endif %}
      </li>
      {% if flight_price_krw %}
        <li>KRW 환산(대략): {{ flight_price_krw }}</li>
      {% endif %}
    </ul>

    {% if fh_hotels %}
      <h3>② 해당 기간 호텔 최저가 TOP {{ fh_hotels|length }}</h3>
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
          항공 (KRW 환산 약 {{ flight_price_krw|default(best_flight.price_value|round(0) if best_flight.price_value is not none else 0) }}) +
          호텔 “{{ combo_hotel.name }}” ({{ combo_hotel.total_price }}) =
          <strong>{{ combined_total|round(0) }}</strong>
          KRW
        </p>
      {% endif %}
    {% else %}
      <p>해당 기간에 대한 호텔 검색 결과가 없습니다.</p>
    {% endif %}
  {% endif %}

  <script>

        function updateModeFields() {
      const checked = document.querySelector('input[name="mode"]:checked');
      const mode = checked ? checked.value : 'flight_hotel_month';

      const fsHotelPeriod = document.getElementById('fs_hotel_period');
      const fsTripPeriod  = document.getElementById('fs_trip_period');
      const fsMonth = document.getElementById('fs_month_common');
      const fsFlight = document.getElementById('fs_flight');

      if (fsHotelPeriod) fsHotelPeriod.classList.toggle('hidden', mode !== 'hotel_period');
      if (fsTripPeriod)  fsTripPeriod.classList.toggle('hidden', mode !== 'flight_hotel_period');
      if (fsMonth)       fsMonth.classList.toggle('hidden', !(mode === 'hotel_month' || mode === 'flight_hotel_month'));
      if (fsFlight)      fsFlight.classList.toggle('hidden', !(mode === 'flight_hotel_month' || mode === 'flight_hotel_period'));
    }

    document.addEventListener('DOMContentLoaded', () => {
      document.querySelectorAll('input[name="mode"]').forEach((el) => {
        el.addEventListener('change', updateModeFields);
      });
      updateModeFields();
    });
  </script>

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

    # ACCESS_KEY가 켜진 경우를 위해 key를 템플릿에 넘겨 폼 POST에 유지
    current_key = request.args.get("key", "")

    def _to_int(v, default: int) -> int:
        try:
            return int(v)
        except Exception:
            return default

    if request.method == "POST":
        form = request.form
        mode = form.get("mode", mode)

        city = form.get("city", city)
        country = form.get("country", country)
        adults = _to_int(form.get("adults", adults), adults)
        min_stars = _to_int(form.get("min_stars", min_stars), min_stars)
        max_stars = _to_int(form.get("max_stars", max_stars), max_stars)
        currency = form.get("currency", currency)
        guest_nat = form.get("guest_nat", guest_nat)

        checkin = form.get("checkin", checkin)
        checkout = form.get("checkout", checkout)
        top_n = _to_int(form.get("top_n", top_n), top_n)
        limit = _to_int(form.get("limit", limit), limit)

        year = _to_int(form.get("year", year), year)
        month = _to_int(form.get("month", month), month)
        nights = _to_int(form.get("nights", nights), nights)

        origin = form.get("origin", origin)
        dest = form.get("dest", dest)
        trip = form.get("trip", trip)
        seat = form.get("seat", seat)
        flight_adults = _to_int(form.get("flight_adults", flight_adults), flight_adults)
        fh_top_n = _to_int(form.get("fh_top_n", fh_top_n), fh_top_n)

        # 간단한 값 범위 방어
        if month < 1:
            month = 1
        if month > 12:
            month = 12
        if nights < 1:
            nights = 1
        if top_n < 1:
            top_n = 1
        if limit < 1:
            limit = 1

        flight_price_krw = None

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


            elif mode == "flight_hotel_period":
                ci = date.fromisoformat(checkin)
                co = date.fromisoformat(checkout)

                best_flight = find_cheapest_flight_for_dates(
                    depart=ci,
                    ret=co if trip == "round-trip" else None,
                    origin=origin,
                    dest=dest,
                    trip=trip,
                    adults=flight_adults,
                    seat=seat,
                )

                # ✅ 항공 KRW 환산
                if best_flight and best_flight.price_value is not None:
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
            
                # ✅ 항공 KRW 환산(항상 best_flight 직후)
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
            
                fh_hotels = []
                combined_total = None
            
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
                        currency=currency,      # 호텔 통화는 이미 KRW로 주는 게 보통
                        nationality=guest_nat,
                    )
            
                    if fh_hotels:
                        fh_hotels = fh_hotels[:fh_top_n]
                        combo_hotel = fh_hotels[0]
            
                        # ✅ 합산은 KRW 기준으로만
                        if flight_price_krw is not None:
                            try:
                                combined_total = int(flight_price_krw) + int(combo_hotel.total_price)
                            except Exception:
                                combined_total = None

        except Exception as e:
            # Render에서는 에러 메시지라도 화면에 보여주는 게 디버깅에 도움이 됨
            error = f"{type(e).__name__}: {e}"

    else:
        # GET에서는 아직 항공권 검색을 돌리지 않고(초기 로딩 빨라짐) 화면만 띄움
        flight_price_krw = None
    return render_template_string(
        TEMPLATE,
        current_key=current_key,
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
        flight_price_krw=flight_price_krw,
        combined_total=combined_total,
        combo_hotel=combo_hotel,
    )


if __name__ == "__main__":
    # Render 등 배포환경에서는 gunicorn이 실행함
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=debug)
