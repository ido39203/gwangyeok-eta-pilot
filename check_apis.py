"""1단계: 신청한 API가 실제로 작동하는지 한 번씩 호출해 확인한다.

실행:  python check_apis.py
결과:
  - 화면에 점검표 출력
  - data/check_results/api_check_YYYYMMDD_HHMM.csv  (엑셀로 열 수 있음)
  - data/check_results/samples/*.json              (API별 응답 예시 1건)
  - data/state/resolved_routes.json                 (찾아낸 노선 ID. collect.py가 사용)
  - data/ref/*.json                                 (노선 정보·경유 정류소 목록. 나중에 급행 구간 정의에 사용)
"""
from __future__ import annotations

import argparse
import csv
from datetime import timedelta
from pathlib import Path
from typing import Optional

import endpoints as ep
from common import (safe_console, BASE_DIR, Http, Paths, Quota, Resp, api_message, as_list, find_key, latlon_to_grid, load_config,
                    load_env, mask, now_kst, read_json, safe_params, safe_url, tago_items, write_json)


def resp_ok(resp: Resp) -> bool:
    """HTTP 200이고 JSON으로 읽혔는지. 이게 아니면 '없음' 같은 판단을 하지 않는다."""
    return resp is not None and resp.status == 200 and resp.json is not None


class Checker:
    def __init__(self, base_dir: Path = BASE_DIR, http: Optional[Http] = None, env: Optional[dict] = None,
                 cfg: Optional[dict] = None, clock=now_kst):
        self.base_dir = base_dir
        self.env = env if env is not None else load_env(base_dir)
        self.cfg = cfg if cfg is not None else load_config(base_dir)
        self.http = http or Http()
        self.clock = clock
        self.paths = Paths(base_dir)
        q = self.cfg.get("quota", {})
        self.quota = Quota(self.paths.state, q.get("limits", {}), q.get("safety_ratio", 0.95), clock)
        self.rows: list[dict] = []
        self.samples_dir = self.paths.checks / "samples"
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        self.resolved: dict = read_json(self.paths.state / "resolved_routes.json", {}) or {}
        self.seoul_base = self.cfg.get("seoul_bus_base", "http://ws.bus.go.kr/api/rest")

    # ------------------------------------------------------------------
    def call(self, group: str, url_params: tuple) -> Resp:
        url, params = url_params
        resp = self.http.get(url, params)
        self.quota.add(group)
        return resp

    def record(self, api: str, what: str, resp: Optional[Resp], items: list, note: str = "",
               ok_if_zero: bool = False, sample_name: str = "") -> str:
        if resp is None:
            result, status, code, msg = "건너뜀", "", "", note
            note = ""
        else:
            code, msg = api_message(resp)
            status = resp.status if resp.status is not None else ""
            if resp.error:
                result = "실패(연결)"
            elif resp.status != 200:
                result = "실패(HTTP)"
            elif resp.json is None:
                result = "실패(응답 형식)"
            elif items:
                result = "성공"
            elif ok_if_zero and code in ("00", "0", "INFO-000"):
                result = "성공(0건)"
            else:
                result = "확인 필요(0건)"
            if sample_name:
                write_json(self.samples_dir / f"{sample_name}.json", {
                    "url": safe_url(resp.url), "params": safe_params(resp.params), "http": resp.status,
                    "json": resp.json, "text": None if resp.json is not None else resp.text[:3000]})
        row = {"순서": len(self.rows) + 1, "API": api, "확인 내용": what, "결과": result, "HTTP": status,
               "코드": code, "메시지": (msg or "")[:80], "건수": len(items) if resp is not None else "",
               "ms": resp.ms if resp is not None else "", "찾은 값/비고": note}
        self.rows.append(row)
        return result

    # ------------------------------------------------------------------
    def run(self) -> list[dict]:
        key = self.env.get("DATA_GO_KR_KEY", "")
        ex_key = self.env.get("EX_KEY", "")
        seoul_key = self.env.get("SEOUL_KEY", "")
        print(f"공공데이터포털 키: {mask(key)} / 도로공사 키: {mask(ex_key)} / 서울 열린데이터광장 키: {mask(seoul_key)}")
        if not key:
            self.record("공공데이터포털", "인증키", None, [], ".env에 DATA_GO_KR_KEY가 없습니다. 버스·기상·공휴일 점검을 건너뜁니다.")
        else:
            for r in self.cfg.get("routes", []):
                if "gbis" in r:
                    self.check_gbis_route(key, r)
                if "tago" in r:
                    self.check_tago_route(key, r)
                if "seoul" in r:
                    self.check_seoul_route(key, r)
                if r.get("gbis_backup_check"):
                    self.check_gbis_backup(key, r)
            self.check_gbis_station(key)
            self.check_weather(key)
            self.check_holidays(key)
            self.check_asos(key)
        self.check_ex(ex_key)
        self.check_seoul_traffic(seoul_key)
        write_json(self.paths.state / "resolved_routes.json", self.resolved)
        return self.rows

    # ---------------- 경기 GBIS ----------------
    def check_gbis_route(self, key: str, r: dict) -> None:
        name = r["name"]
        g = r["gbis"]
        rid = str(g.get("route_id") or "")
        info: dict = self.resolved.setdefault(name, {})
        if not rid:
            resp = self.call("gbis_busroute", ep.gbis_route_search(key, g.get("keyword", name)))
            items = as_list(find_key(resp.json, "busRouteList"))
            exact = [i for i in items if str(i.get("routeName")) == name]
            pick = exact[0] if exact else None
            note = ""
            if pick:
                rid = str(pick.get("routeId"))
                info.update({"gbis_route_id": rid, "gbis_route_type_cd": pick.get("routeTypeCd"),
                             "gbis_route_type_name": pick.get("routeTypeName"), "gbis_region": pick.get("regionName")})
                note = f"routeId={rid}, 유형={pick.get('routeTypeCd')} {pick.get('routeTypeName', '')}"
                if len(exact) > 1:
                    note += f" (같은 번호 {len(exact)}개 중 첫 번째. config에 route_id를 직접 적어 고정하세요)"
            elif items:
                note = f"이름이 정확히 같은 노선 없음. 후보: {', '.join(str(i.get('routeName')) for i in items[:5])}"
            self.record("경기 버스노선 조회", f"{name} 노선 찾기", resp, exact, note, sample_name="gbis_route_search")
        else:
            info["gbis_route_id"] = rid
            self.record("경기 버스노선 조회", f"{name} 노선 찾기", None, [], f"config에 route_id={rid} 고정됨")
        if not rid:
            return

        resp = self.call("gbis_busroute", ep.gbis_route_info(key, rid))
        item = find_key(resp.json, "busRouteInfoItem")
        items = as_list(item)
        note = ""
        if items:
            it = items[0]
            note = (f"첫차 {it.get('upFirstTime')} 막차 {it.get('upLastTime')} / "
                    f"배차 {it.get('peekAlloc')}~{it.get('nPeekAlloc')}분")
            write_json(self.paths.ref / f"gbis_route_info_{name}.json", it)
        self.record("경기 버스노선 조회", f"{name} 노선 정보(첫·막차, 배차)", resp, items, note, sample_name="gbis_route_info")

        resp = self.call("gbis_busroute", ep.gbis_route_stations(key, rid))
        stations = as_list(find_key(resp.json, "busRouteStationList"))
        note = ""
        if stations:
            write_json(self.paths.ref / f"gbis_route_stations_{name}.json", stations)
            regions = []
            for s in stations:
                rg = s.get("regionName")
                if rg and (not regions or regions[-1] != rg):
                    regions.append(rg)
            note = f"정류소 {len(stations)}개, 지나는 지역 순서: {' > '.join(regions[:8])}"
            info["gbis_first_station_id"] = str(stations[0].get("stationId"))
        self.record("경기 버스노선 조회", f"{name} 경유 정류소", resp, stations, note, sample_name="gbis_route_stations")

        resp = self.call("gbis_buslocation", ep.gbis_bus_location(key, rid))
        buses = as_list(find_key(resp.json, "busLocationList"))
        note = ""
        if buses:
            note = f"운행 중 {len(buses)}대, 예) {buses[0].get('plateNo')} 순번 {buses[0].get('stationSeq')} 빈자리 {buses[0].get('remainSeatCnt')}"
        elif resp_ok(resp):
            note = "운행 시간이 아니면 0건일 수 있음"
        self.record("경기 버스위치정보", f"{name} 버스 위치", resp, buses, note, sample_name="gbis_bus_location")

        sid = info.get("gbis_first_station_id")
        if sid:
            resp = self.call("gbis_arrival", ep.gbis_arrival_list(key, sid))
            arr = as_list(find_key(resp.json, "busArrivalList"))
            self.record("경기 버스도착정보", f"{name} 기점 정류소 도착정보", resp, arr,
                        f"정류소 {sid}" + ("" if arr else " / 운행 시간이 아니면 0건일 수 있음"), sample_name="gbis_arrival")

    def check_gbis_backup(self, key: str, r: dict) -> None:
        name = r["name"]
        resp = self.call("gbis_busroute", ep.gbis_route_search(key, name))
        items = as_list(find_key(resp.json, "busRouteList"))
        exact = [i for i in items if str(i.get("routeName")) == name]
        if exact:
            note = f"GBIS에서도 조회됨 routeId={exact[0].get('routeId')} → 서울 노선 백업 후보"
        elif resp_ok(resp):
            note = "GBIS에서는 조회되지 않음 → 서울 노선 백업 없음"
        else:
            note = ""
        if exact:
            self.resolved.setdefault(name, {})["gbis_backup_route_id"] = str(exact[0].get("routeId"))
        self.record("경기 버스노선 조회", f"{name}(서울 노선) GBIS 조회 여부", resp, exact, note, ok_if_zero=True,
                    sample_name="gbis_backup_search")

    def check_gbis_station(self, key: str) -> None:
        resp = self.call("gbis_station", ep.gbis_station_search(key, "사당역"))
        items = as_list(find_key(resp.json, "busStationList"))
        self.record("경기도 정류소 조회", "'사당역' 정류소 검색", resp, items,
                    f"{len(items)}개 정류소" if items else "", sample_name="gbis_station_search")

    # ---------------- TAGO ----------------
    def check_tago_route(self, key: str, r: dict) -> None:
        name = r["name"]
        t = r["tago"]
        info: dict = self.resolved.setdefault(name, {})
        rid = str(t.get("route_id") or "")
        city = None
        if rid:
            city = (t.get("city_codes") or [None])[0]
            self.record("TAGO 버스노선정보", f"{name} 노선 찾기", None, [], f"config에 route_id={rid} 고정됨")
        else:
            for c in t.get("city_codes", []):
                resp = self.call("tago_route", ep.tago_route_no(key, c, name))
                items = tago_items(resp.json)
                exact = [i for i in items if str(i.get("routeno")).strip() == name]
                if exact:
                    rid, city = str(exact[0].get("routeid")), c
                    note = f"도시 {c}에서 routeid={rid} ({exact[0].get('routetp', '')})"
                    self.record("TAGO 버스노선정보", f"{name} 노선 찾기", resp, exact, note, sample_name="tago_route_no")
                    break
                cand = ", ".join(str(i.get("routeno")) for i in items[:5])
                note = ("이 도시에서는 없음" + (f". 비슷한 번호: {cand}" if cand else "")) if resp_ok(resp) else ""
                self.record("TAGO 버스노선정보", f"{name} 노선 찾기 (도시 {c})", resp, exact, note, sample_name="tago_route_no")
        if not rid:
            return
        info.update({"tago_route_id": rid, "tago_city_code": city})

        resp = self.call("tago_route", ep.tago_route_stations(key, city, rid))
        sts = tago_items(resp.json)
        note = ""
        if sts:
            write_json(self.paths.ref / f"tago_route_stations_{name}.json", sts)
            skip = sum(1 for s in sts if "미정차" in str(s.get("nodenm", "")))
            note = f"정류소 {len(sts)}개" + (f", 그중 '(미정차)' 통과 지점 {skip}개" if skip else "")
            info["tago_first_node_id"] = str(sts[0].get("nodeid"))
        self.record("TAGO 버스노선정보", f"{name} 경유 정류소", resp, sts, note, sample_name="tago_route_stations")

        resp = self.call("tago_buslc", ep.tago_bus_location(key, city, rid))
        buses = tago_items(resp.json)
        note = (f"운행 중 {len(buses)}대, 예) {buses[0].get('vehicleno')} 순번 {buses[0].get('nodeord')}" if buses
                else ("운행 시간이 아니면 0건일 수 있음" if resp_ok(resp) else ""))
        self.record("TAGO 버스위치정보", f"{name} 버스 위치", resp, buses, note, sample_name="tago_bus_location")

        nid = info.get("tago_first_node_id")
        if nid:
            resp = self.call("tago_arvl", ep.tago_arrival(key, city, nid))
            arr = tago_items(resp.json)
            self.record("TAGO 버스도착정보", f"{name} 기점 정류소 도착정보", resp, arr,
                        f"정류소 {nid}" + ("" if arr else " / 운행 시간이 아니면 0건일 수 있음"), sample_name="tago_arrival")

    # ---------------- 서울 ----------------
    def check_seoul_route(self, key: str, r: dict) -> None:
        name = r["name"]
        s = r["seoul"]
        base = self.seoul_base
        info: dict = self.resolved.setdefault(name, {})
        rid = str(s.get("route_id") or "")
        if not rid:
            resp = self.call("seoul_busroute", ep.seoul_route_list(key, s.get("keyword", name), base))
            items = as_list(find_key(resp.json, "itemList"))
            exact = [i for i in items if str(i.get("busRouteNm")) == name]
            note = ""
            if exact:
                rid = str(exact[0].get("busRouteId"))
                info.update({"seoul_route_id": rid, "seoul_route_type": exact[0].get("routeType")})
                note = f"busRouteId={rid}, routeType={exact[0].get('routeType')} (6=광역)"
            self.record("서울 노선정보조회", f"{name} 노선 찾기", resp, exact, note, sample_name="seoul_route_list")
        else:
            info["seoul_route_id"] = rid
            self.record("서울 노선정보조회", f"{name} 노선 찾기", None, [], f"config에 route_id={rid} 고정됨")
        if not rid:
            return

        resp = self.call("seoul_busroute", ep.seoul_route_stations(key, rid, base))
        sts = as_list(find_key(resp.json, "itemList"))
        note = ""
        if sts:
            write_json(self.paths.ref / f"seoul_route_stations_{name}.json", sts)
            info["seoul_first_ars_id"] = str(sts[0].get("arsId"))
            note = f"정류소 {len(sts)}개, 첫 정류소 {sts[0].get('stationNm')} ({sts[0].get('arsId')})"
        self.record("서울 노선정보조회", f"{name} 경유 정류소", resp, sts, note, sample_name="seoul_route_stations")

        resp = self.call("seoul_buspos", ep.seoul_bus_pos(key, rid, base))
        buses = as_list(find_key(resp.json, "itemList"))
        note = (f"운행 중 {len(buses)}대, 예) {buses[0].get('plainNo')} 구간순번 {buses[0].get('sectOrd')} 도착여부 {buses[0].get('stopFlag')}"
                if buses else ("운행 시간이 아니면 0건일 수 있음" if resp_ok(resp) else ""))
        self.record("서울 버스위치정보조회", f"{name} 버스 위치", resp, buses, note, sample_name="seoul_bus_pos")

        resp = self.call("seoul_arrive", ep.seoul_arrival_route(key, rid, base))
        arr = as_list(find_key(resp.json, "itemList"))
        self.record("서울 버스도착정보조회", f"{name} 전 정류소 도착정보", resp, arr, "", sample_name="seoul_arrival")

        ars = info.get("seoul_first_ars_id")
        if ars and ars not in ("0", "None", ""):
            resp = self.call("seoul_station", ep.seoul_station_by_uid(key, ars, base))
            items = as_list(find_key(resp.json, "itemList"))
            self.record("서울 정류소정보조회", f"정류소 {ars} 정보", resp, items, "", sample_name="seoul_station")
        else:
            self.record("서울 정류소정보조회", "정류소 정보", None, [], "첫 정류소 arsId가 없어 건너뜀 (경기 소재 정류소일 수 있음)")

    # ---------------- 기상·공휴일 ----------------
    def check_weather(self, key: str) -> None:
        pts = self.cfg.get("weather_points") or [{"name": "서울역", "lat": 37.5547, "lon": 126.9707}]
        p = pts[0]
        nx, ny = latlon_to_grid(p["lat"], p["lon"])
        now = self.clock()
        base = now.replace(minute=0, second=0, microsecond=0)
        if now.minute < 45:
            base -= timedelta(hours=1)
        resp = self.call("kma_vilage", ep.kma_ultra_ncst(key, base.strftime("%Y%m%d"), base.strftime("%H%M"), nx, ny))
        items = tago_items(resp.json)
        cats = ", ".join(f"{i.get('category')}={i.get('obsrValue')}" for i in items[:8])
        self.record("기상청 단기예보(초단기실황)", f"{p['name']} 격자({nx},{ny}) {base:%H}시 실황", resp, items, cats,
                    sample_name="kma_ultra_ncst")

    def check_holidays(self, key: str) -> None:
        now = self.clock()
        resp = self.call("kasi_spcde", ep.kasi_holidays(key, now.year, now.month))
        items = tago_items(resp.json)
        note = ", ".join(f"{i.get('locdate')} {i.get('dateName')}" for i in items[:5]) or ("이번 달 공휴일 없음" if resp_ok(resp) else "")
        self.record("천문연 특일정보", f"{now:%Y년 %m월} 공휴일", resp, items, note, ok_if_zero=True, sample_name="kasi_holidays")

    def check_asos(self, key: str) -> None:
        stn = int(self.cfg.get("asos_check_station", 119))
        day = (self.clock() - timedelta(days=1)).strftime("%Y%m%d")
        resp = self.call("kma_asos", ep.kma_asos_hourly(key, stn, day))
        items = tago_items(resp.json)
        note = "" if items else "기관 전용이라 승인이 안 됐을 수 있음. 실패해도 기상자료개방포털로 대체 가능"
        self.record("기상청 ASOS 시간자료", f"관측소 {stn} 어제 시간자료", resp, items, note, sample_name="kma_asos")

    # ---------------- 도로공사 ----------------
    def check_ex(self, ex_key: str) -> None:
        if not ex_key:
            self.record("도로공사 실시간 소통", "0405", None, [], ".env에 EX_KEY가 없어 건너뜀")
            return
        rcfg = self.cfg.get("road_ex", {})
        url = (rcfg.get("realtime_url") or "").strip()
        if url:
            resp = self.call("ex_realtime", ep.ex_realtime(ex_key, url, rcfg.get("extra_params")))
            rows = as_list(find_key(resp.json, "list"))
            note = ""
            if rows:
                routes = sorted({(str(x.get("routeNo")), str(x.get("routeName"))) for x in rows})
                note = f"콘존 {len(rows)}행, 고속도로 {len(routes)}개, stdHour 예) {rows[0].get('stdHour')}"
                write_json(self.paths.ref / "ex_route_list.json", [{"routeNo": a, "routeName": b} for a, b in routes])
            self.record("도로공사 실시간 소통(0405)", "전국 콘존 소통", resp, rows, note, sample_name="ex_realtime")
        else:
            self.record("도로공사 실시간 소통(0405)", "전국 콘존 소통", None, [],
                        "config.yaml의 road_ex.realtime_url이 비어 있어 건너뜀")
        resp = self.call("ex_congest", ep.ex_congest(ex_key))
        rows = as_list(find_key(resp.json, "list"))
        self.record("도로공사 정체상황(0406)", "정체 콘존", resp, rows, "0405의 부분집합이라 수집에는 쓰지 않음",
                    ok_if_zero=True, sample_name="ex_congest")

    # ---------------- 서울 도로 소통 ----------------
    def check_seoul_traffic(self, seoul_key: str) -> None:
        if not seoul_key:
            self.record("서울 실시간 도로 소통", "OA-13291", None, [], ".env에 SEOUL_KEY가 없어 건너뜀")
            return
        st = self.cfg.get("seoul_traffic", {})
        service = st.get("service", "TrafficInfo")
        link = str(st.get("test_link_id") or "")
        resp = self.call("seoul_traffic", ep.seoul_open(seoul_key, service, 1, 5, link))
        rows = as_list(find_key(resp.json, "row"))
        if link:
            note = ", ".join(f"{k}={v}" for k, v in (rows[0].items() if rows else []))[:120]
        else:
            note = "링크 ID 없이 키만 확인함. 메시지가 '인증키' 오류가 아니면 키는 정상. test_link_id를 채우고 다시 실행"
        self.record("서울 실시간 도로 소통", f"{service} 링크 {link or '(없음)'}", resp, rows, note,
                    sample_name="seoul_traffic")


# ----------------------------------------------------------------------
def print_table(rows: list[dict]) -> None:
    cols = ["순서", "결과", "API", "확인 내용", "건수", "코드", "찾은 값/비고"]
    print()
    print(" | ".join(cols))
    print("-" * 100)
    for r in rows:
        vals = [str(r.get(c, "")) for c in cols]
        if r.get("결과", "").startswith("실패") and r.get("메시지"):
            vals[-1] = (vals[-1] + " / 메시지: " + str(r["메시지"])).strip(" /")
        print(" | ".join(vals))
    ok = sum(1 for r in rows if r["결과"].startswith("성공"))
    bad = sum(1 for r in rows if r["결과"].startswith("실패"))
    warn = sum(1 for r in rows if r["결과"].startswith("확인"))
    skip = sum(1 for r in rows if r["결과"] == "건너뜀")
    print("-" * 100)
    print(f"성공 {ok} / 확인 필요 {warn} / 실패 {bad} / 건너뜀 {skip}")


def save_csv(rows: list[dict], out_dir: Path, when) -> Path:
    p = out_dir / f"api_check_{when:%Y%m%d_%H%M}.csv"
    cols = ["순서", "API", "확인 내용", "결과", "HTTP", "코드", "메시지", "건수", "ms", "찾은 값/비고"]
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return p


def main() -> None:
    safe_console()
    ap = argparse.ArgumentParser(description="신청한 API를 한 번씩 호출해 작동 여부를 확인합니다.")
    ap.parse_args()
    chk = Checker()
    rows = chk.run()
    print_table(rows)
    p = save_csv(rows, chk.paths.checks, chk.clock())
    print(f"\n점검표 저장: {p}")
    print(f"찾은 노선 ID: {chk.paths.state / 'resolved_routes.json'}")
    print(f"오늘 호출 수: {chk.quota.snapshot()}")


if __name__ == "__main__":
    main()
