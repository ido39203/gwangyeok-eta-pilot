"""대상 노선 후보 조회: 후보 노선마다 경기 버스노선 API로 유형·기종점·배차·경유 정류소를 확인한다.

실행:  python route_candidates.py
결과:  data/ref/route_candidates.csv          후보 노선 요약 (엑셀로 열림)
       data/ref/candidates/<노선>_stations.json  경유 정류소 원본

- 경기 버스노선 API(gbis_busroute)만 쓴다. 버스위치 API 한도에는 영향이 없다.
- 노선당 최대 3회 호출(검색, 노선 정보, 경유 정류소). 후보 63개면 약 190회.
- 호출 수는 수집기와 같은 장부(data/state/quota_날짜.json)에 기록된다.
- 후보 목록은 경기 노선별 이용객수(2025년 평일 시간대별)에서 출근 06~10시 이용객이 많고
  서울행으로 보이는(또는 기종점이 비어 있는) 노선을 뽑은 것이다.
"""
from __future__ import annotations

import csv
import time

import endpoints as ep
from common import BASE_DIR, Http, Paths, Quota, as_list, find_key, load_config, load_env, safe_console, write_json

CANDIDATES = [
    ("수원시", "7770"), ("고양시", "1000"), ("용인시", "1500-2"), ("수원시", "7800"), ("용인시", "8100"),
    ("수원시", "7780"), ("평택시", "1220"), ("수원시", "M5107"), ("성남시", "3500"), ("시흥시", "5602"),
    ("용인시", "1151"), ("수원시", "5100"), ("용인시", "5600"), ("성남시", "8106"), ("수원시", "2007"),
    ("고양시", "1200"), ("양주시", "G1300"), ("포천시", "3100"), ("화성시", "1007"), ("김포시", "1002"),
    ("남양주시", "8002"), ("고양시", "1500"), ("수원시", "1112"), ("용인시", "5005"), ("하남시", "9302"),
    ("성남시", "9007"), ("시흥시", "5601"), ("하남시", "3000"), ("광주시", "1113-1"), ("수원시", "M5121"),
    ("성남시", "9300"), ("용인시", "M4101"), ("수원시", "8800"), ("오산시", "1550-1"), ("용인시", "5500-2"),
    ("화성시", "1009"), ("수원시", "3000"), ("화성시", "6002"), ("수원시", "3007"), ("수원시", "3003"),
    ("수원시", "7000"), ("화성시", "M4130"), ("화성시", "M4403"), ("화성시", "M4137"), ("용인시", "1150"),
    ("성남시", "G8110"), ("성남시", "M4102"), ("광명시", "G9633"), ("광명시", "8507"), ("용인시", "1117"),
    ("화성시", "8155"), ("하남시", "9303"), ("화성시", "7790"), ("하남시", "9301"), ("고양시", "1082"),
    ("수원시", "7001"), ("남양주시", "M2323"), ("고양시", "M7731"), ("오산시", "5300"), ("시흥시", "5200"),
    ("성남시", "9003"), ("화성시", "M4108"), ("용인시", "6900"),
]

FIELDS = ["시군", "노선", "결과", "routeId", "routeTypeCd", "routeTypeName", "adminName", "기점", "종점",
          "regionName", "서울경유", "첨두배차(분)", "비첨두배차(분)", "첫차", "막차",
          "상행정류장수", "상행정차정류장수", "출발시군정차수", "서울첫정류장순번", "회차순번", "메모"]


def main() -> None:
    safe_console()
    env = load_env()
    key = env.get("DATA_GO_KR_KEY", "")
    if not key:
        print(".env에 DATA_GO_KR_KEY가 없습니다.")
        return
    cfg = load_config()
    paths = Paths(BASE_DIR)
    q = cfg.get("quota", {})
    quota = Quota(paths.state, q.get("limits", {}), q.get("safety_ratio", 0.95))
    http = Http()
    out_dir = paths.ref / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    def call(url_params):
        if not quota.allow("gbis_busroute"):
            raise SystemExit("경기 버스노선 API 오늘 한도에 닿아 멈춥니다. 내일 다시 실행하세요.")
        url, params = url_params
        resp = http.get(url, params)
        quota.add("gbis_busroute")
        time.sleep(0.2)
        return resp

    rows = []
    for i, (city, name) in enumerate(CANDIDATES, 1):
        row = {k: "" for k in FIELDS}
        row.update({"시군": city, "노선": name})
        print(f"[{i}/{len(CANDIDATES)}] {city} {name}", end=" ... ", flush=True)

        resp = call(ep.gbis_route_search(key, name))
        items = as_list(find_key(resp.json, "busRouteList"))
        exact = [it for it in items if str(it.get("routeName")) == name]
        same_city = [it for it in exact if city in str(it.get("adminName", ""))]
        pick = (same_city or exact or [None])[0]
        if pick is None:
            row["결과"] = "노선 없음" if resp.status == 200 else f"실패(HTTP {resp.status})"
            print(row["결과"])
            rows.append(row)
            continue
        if len(exact) > 1:
            row["메모"] = f"같은 번호 {len(exact)}개" + ("" if same_city else ", 시군 일치 없음 - 확인 필요")
        rid = str(pick.get("routeId"))
        row.update({"routeId": rid, "routeTypeCd": pick.get("routeTypeCd"), "routeTypeName": pick.get("routeTypeName"),
                    "adminName": pick.get("adminName"), "regionName": pick.get("regionName")})

        resp = call(ep.gbis_route_info(key, rid))
        info = (as_list(find_key(resp.json, "busRouteInfoItem")) or [{}])[0]
        row.update({"기점": info.get("startStationName", ""), "종점": info.get("endStationName", ""),
                    "첨두배차(분)": info.get("peekAlloc", ""), "비첨두배차(분)": info.get("nPeekAlloc", ""),
                    "첫차": info.get("upFirstTime", ""), "막차": info.get("upLastTime", "")})
        region = str(info.get("regionName") or row["regionName"] or "")
        row["regionName"] = region
        row["서울경유"] = "Y" if "서울" in region else "N"

        resp = call(ep.gbis_route_stations(key, rid))
        stations = as_list(find_key(resp.json, "busRouteStationList"))
        if stations:
            write_json(out_dir / f"{name}_stations.json", stations)
            stations.sort(key=lambda s: int(s.get("stationSeq", 0)))
            turn = int(stations[0].get("turnSeq") or 0) or len(stations)
            up = [s for s in stations if int(s.get("stationSeq", 0)) <= turn]
            stop = [s for s in up if "미정차" not in str(s.get("stationName", ""))]
            home = city[:-1]  # 수원시 -> 수원
            seoul = [s for s in up if s.get("regionName") == "서울"]
            row.update({"상행정류장수": len(up), "상행정차정류장수": len(stop),
                        "출발시군정차수": sum(1 for s in stop if s.get("regionName") == home),
                        "서울첫정류장순번": seoul[0].get("stationSeq") if seoul else "", "회차순번": turn})
        row["결과"] = "성공"
        print(f"{row['routeTypeName']} / {row['기점']} → {row['종점']} / 서울경유 {row['서울경유']}")
        rows.append(row)

    out = paths.ref / "route_candidates.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    ok = sum(1 for r in rows if r["결과"] == "성공")
    print(f"\n완료: {ok}/{len(rows)}개 조회 성공. 결과 파일: {out}")


if __name__ == "__main__":
    main()
