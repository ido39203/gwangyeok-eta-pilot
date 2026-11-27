"""경기 버스노선 API로 노선 번호를 검색해 같은 번호가 들어간 노선의 routeId·기점·종점을 모두 보여 준다.

실행:  python find_route.py 5005
       python find_route.py --save 228000447 "5005(거점)"   경유 정류소를 data/ref/gbis_route_stations_<이름>.json으로 저장
- 경기 버스노선 API(gbis_busroute)만 쓴다. 버스위치 API 한도에는 영향이 없다.
- 검색 1회 + 찾은 노선마다 노선 정보 1회.
"""
from __future__ import annotations

import sys
import time

import endpoints as ep
from common import BASE_DIR, Http, Paths, Quota, as_list, find_key, load_config, load_env, safe_console, write_json


def main() -> None:
    safe_console()
    if len(sys.argv) < 2:
        print("사용법: python find_route.py 5005")
        return
    name = sys.argv[1]
    if name == "--save" and len(sys.argv) < 4:
        print('사용법: python find_route.py --save 228000447 "5005(거점)"')
        return
    key = load_env().get("DATA_GO_KR_KEY", "")
    if not key:
        print(".env에 DATA_GO_KR_KEY가 없습니다.")
        return
    cfg = load_config()
    q = cfg.get("quota", {})
    quota = Quota(Paths(BASE_DIR).state, q.get("limits", {}), q.get("safety_ratio", 0.95))
    http = Http()

    def call(url_params):
        if not quota.allow("gbis_busroute"):
            raise SystemExit("경기 버스노선 API 오늘 한도에 닿아 멈춥니다.")
        url, params = url_params
        resp = http.get(url, params)
        quota.add("gbis_busroute")
        time.sleep(0.2)
        return resp

    if name == "--save":
        rid, rname = sys.argv[2], sys.argv[3]
        resp = call(ep.gbis_route_stations(key, rid))
        stations = as_list(find_key(resp.json, "busRouteStationList"))
        if not stations:
            print(f"정류소 목록을 받지 못했습니다 (HTTP {resp.status})")
            return
        stations.sort(key=lambda s: int(s.get("stationSeq", 0)))
        out = Paths(BASE_DIR).ref / f"gbis_route_stations_{rname}.json"
        write_json(out, stations)
        print(f"저장: {out} (정류소 {len(stations)}개, 회차 순번 {stations[0].get('turnSeq')})")
        for s_ in stations:
            print(f"  {s_.get('stationSeq')}\t{s_.get('stationName')}\t{s_.get('regionName')}")
        return

    resp = call(ep.gbis_route_search(key, name))
    items = as_list(find_key(resp.json, "busRouteList"))
    if not items:
        print(f"검색 결과 없음 (HTTP {resp.status})")
        return
    print(f"'{name}' 검색 결과 {len(items)}개\n")
    for it in items:
        rid = str(it.get("routeId"))
        info_resp = call(ep.gbis_route_info(key, rid))
        info = (as_list(find_key(info_resp.json, "busRouteInfoItem")) or [{}])[0]
        print(f"{it.get('routeName')}\trouteId={rid}\t{it.get('routeTypeName')}\t{it.get('adminName')}\t"
              f"{info.get('startStationName', '')} → {info.get('endStationName', '')}\t"
              f"첫차 {info.get('upFirstTime', '')} 막차 {info.get('upLastTime', '')}")


if __name__ == "__main__":
    main()
