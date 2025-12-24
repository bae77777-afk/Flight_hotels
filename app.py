from __future__ import annotations

import os
import calendar
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional

from flask import Flask, request, render_template

from Hotel_flight import (
    build_payload_for_period,
    fetch_rates,
    build_rows_from_rates,
    get_min_price_for_date_via_helper,
    search_hotels_for_dates,
    find_cheapest_flight_in_month,
)

USD_TO_KRW = 1350.0  # 대충 환율(필요하면 조정)

# 환경변수
LITEAPI_KEY = os.environ.get("LITEAPI_KEY")
ACCESS_KEY = os.environ.get("ACCESS_KEY")  # optional
SECRET_KEY = os.environ.get("SECRET_KEY")  # optional

app = Flask(__name__)


def _parse_price_value(price_str: str) -> Optional[float]:
    """
    "$1,234" / "₩ 123,000" / "123,456" 같은 문자열에서 숫자만 추출해서 float 반환
    """
    if not price_str:
        return None
    s = price_str.strip()
    # 숫자/쉼표/소수점만 남기기
    import re

    nums = re.findall(r"[\d,.]+", s)
    if not nums:
        return None
    raw = nums[0].replace(",", "")
    try:
        return float(raw)
    except Exception:
        return None


# =========================
# ✅ 최종: fallback → (401/토큰)면 common 재시도 → 실패면 None
# =========================
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
    - 기본은 fetch_mode="fallback" 시도
    - fallback이 401(no token) 등으로 실패하면 fetch_mode="common"으로 1회 재시도
    - 최종 실패 시 None 반환 (페이지 전체가 죽지 않게)
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

    passengers = Passengers(adults=adults)

    def _run(mode: str):
        return get_flights(
            flight_data=flight_data,
            trip=trip,
            seat=seat,
            passengers=passengers,
            fetch_mode=mode,
        )

    # 1) fallback 우선
    try:
        result = _run("fallback")
    except AssertionError as e:
        msg = str(e)
        # 401 / no token provided → common으로 재시도
        if ("401" in msg) or ("no token provided" in msg) or ("token" in msg and "error" in msg):
            try:
                result = _run("common")
            except Exception:
                return None
        else:
            # 다른 AssertionError도 common 한번 시도
            try:
                result = _run("common")
            except Exception:
                return None
    except Exception:
        # fallback 자체가 불안정하면 common으로 1회 재시도
        try:
            result = _run("common")
        except Exception:
            return None

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


@app.get("/")
def index():
    return render_template("index.html", title="소개")


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
    limit = _int("limit", 50)

    # 여행기간 입력
    checkin_s = request.form.get("checkin") or "2026-01-10"
    checkout_s = request.form.get("checkout") or "2026-01-14"
    top_n = _int("top_n", 10)

    # 한달 검색
    year = _int("year", date.today().year)
    month = _int("month", date.today().month)
    nights = _int("nights", 3)

    # 항공 설정
    origin = request.form.get("origin") or "ICN"
    dest = request.form.get("dest") or "NRT"
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
                # 이 달(연/월)에서 최저가 1건 + 전체 스캔(원하면)
                hotel_cheapest, monthly_results = get_min_price_for_date_via_helper(
                    year=year,
                    month=month,
                    city=city,
                    country=country,
                    nights=nights,
                    min_stars=min_stars,
                    max_stars=max_stars,
                    adults=adults,
                    guest_nationality=guest_nat,
                    currency=currency,
                    limit=limit,
                )

            elif mode == "flight_hotel_period":
                # ✅ 호텔은 먼저: 항공이 실패해도 호텔 결과는 보여주기
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

                # 입력한 checkin/checkout을 항공 날짜로 사용
                depart = checkin
                ret = checkout if trip == "round-trip" else None

                # ✅ 항공: fallback→common 자동 재시도(함수 내부) + 여기서도 방어
                try:
                    best_flight = find_cheapest_flight_for_dates(
                        depart=depart,
                        ret=ret,
                        origin=origin,
                        dest=dest,
                        trip=trip,
                        adults=flight_adults,
                        seat=seat,
                    )
                except Exception as e:
                    # 항공만 실패 처리(페이지는 유지)
                    if error:
                        error = f"{error} / 항공 검색 실패: {type(e).__name__}: {e}"
                    else:
                        error = f"항공 검색 실패: {type(e).__name__}: {e}"
                    best_flight = None

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
        error=error,
        period_rows=period_rows,
        monthly_results=monthly_results,
        hotel_cheapest=hotel_cheapest,
        best_flight=best_flight,
        fh_hotels=fh_hotels,
        combo_hotel=combo_hotel,
        combined_total=combined_total,
        flight_price_krw=flight_price_krw,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)


