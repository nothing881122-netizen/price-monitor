#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flight_probe.py — GitHub Actions(데이터센터 IP)에서 Google Flights 차단 여부 테스트.

목적: flight_monitor 를 클라우드(Actions)로 옮길 수 있는지 확인.
- 알구몬(price_monitor)은 데이터센터 IP 허용 → Actions OK
- Google Flights 는 데이터센터 IP 를 차단할 수 있음 → 이 스크립트로 실측
fetch 만 함 (push/deploy 없음). 3개 노선 round-trip 조회 성공률로 판정.
"""
import re
import sys
from datetime import date, timedelta

try:
    from fast_flights import FlightData, Passengers, get_flights
except ImportError:
    sys.exit("fast-flights 미설치")


def parse_price(t):
    if not t:
        return None
    n = re.sub(r"[^\d]", "", t)
    n = int(n) if n else 0
    return n if 30_000 <= n <= 30_000_000 else None


def probe(o, d, stay, dep):
    ret = dep + timedelta(days=stay)
    try:
        r = get_flights(
            flight_data=[
                FlightData(date=dep.isoformat(), from_airport=o, to_airport=d),
                FlightData(date=ret.isoformat(), from_airport=d, to_airport=o),
            ],
            trip="round-trip", seat="economy", passengers=Passengers(adults=1),
        )
        prices = sorted([p for f in r.flights if (p := parse_price(f.price))])
        if prices:
            return ("OK", f"{len(r.flights)}편, 최저 {prices[0]:,}원, 등급 {r.current_price}")
        return ("EMPTY", f"항공편 {len(r.flights)}개지만 가격 파싱 0")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {str(e)[:120]}")


def main():
    today = date.today()
    dep = today + timedelta(days=40)
    tests = [("ICN", "KIX", 3), ("ICN", "NRT", 3), ("ICN", "LAX", 8)]
    print(f"[probe] {today} 기준 출발일 {dep} round-trip 조회\n", flush=True)
    ok = 0
    for o, d, stay in tests:
        status, msg = probe(o, d, stay, dep)
        print(f"[{status}] {o}->{d}: {msg}", flush=True)
        if status == "OK":
            ok += 1
    print(f"\n결과: {ok}/{len(tests)} 성공", flush=True)
    if ok >= 2:
        print("=> 차단 안 됨. 클라우드(Actions) 전환 가능!", flush=True)
    elif ok == 1:
        print("=> 부분 성공. 불안정 — 재시도 보강 필요할 수 있음", flush=True)
    else:
        print("=> 전부 실패. Google Flights 가 Actions IP 차단 — 클라우드 부적합", flush=True)


if __name__ == "__main__":
    main()
