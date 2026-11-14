"""설치 확인용 자체 점검. 인터넷·인증키 없이 가짜 응답으로 전체 흐름을 돌려 본다.

실행:  python selftest.py
끝에 '전체 통과'가 나오면 코드와 라이브러리 설치는 정상이다.
(실제 API가 되는지는 check_apis.py로 확인한다.)
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import check_apis
import collect
import daily_report
from common import (BASE_DIR, KST, Paths, Resp, api_message, latlon_to_grid, read_jsonl, safe_console, window_interval,
                    epoch_slot)


def expected_slots(windows: list, times: list, offday: bool) -> tuple[int, str]:
    """config 시간표대로라면 times 동안 몇 번 호출해야 하는지와 그때 쓰인 간격(예: '1분').
    2026-11-14: 기대값을 config에서 계산하게 바꿈(이전에는 2분·300초를 직접 적어 두어 설정을 바꿀 때마다 어긋났다)."""
    slots, secs = set(), set()
    for t in times:
        w = window_interval(windows, t, offday)
        if w:
            slots.add((w[0], epoch_slot(t, w[0], w[1])))
            secs.add(w[0])
    label = "·".join(f"{x // 60}분" if x % 60 == 0 else f"{x}초" for x in sorted(secs)) or "해당 없음"
    return len(slots), label

FAKE_KEY = "FAKEKEY_DO_NOT_STORE_1234567890"


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FakeHttp:
    """URL을 보고 그럴듯한 응답을 돌려준다. 호출 수를 세어 버스가 움직이는 것처럼 만든다."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.calls = 0
        self.by_kind: dict = {}

    def _n(self, kind: str) -> int:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        return self.by_kind[kind]

    def get(self, url: str, params: dict | None = None) -> Resp:
        params = params or {}
        self.calls += 1
        body = self.route(url, params)
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        return Resp(url, params, 200, text, None if isinstance(body, str) else json.loads(text), 12)

    def route(self, url: str, p: dict):
        gb_ok = {"resultCode": 0, "resultMessage": "정상적으로 처리되었습니다.", "queryTime": "x"}
        if "getBusRouteListv2" in url:
            kw = p.get("keyword")
            lst = {"7770": [{"routeId": 234000026, "routeName": "7770", "routeTypeCd": 11, "routeTypeName": "직행좌석형시내버스", "regionName": "수원,서울"}],
                   "M5107": [{"routeId": 234001243, "routeName": "M5107", "routeTypeCd": 14, "routeTypeName": "광역급행형시내버스", "regionName": "수원,용인,서울"}]}.get(kw, [])
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busRouteList": lst}}}
        if "getBusRouteInfoItemv2" in url:
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busRouteInfoItem": {"routeId": p["routeId"], "upFirstTime": "05:00", "upLastTime": "23:30", "peekAlloc": 8, "nPeekAlloc": 15}}}}
        if "getBusRouteStationListv2" in url:
            sts = [{"stationSeq": i, "stationId": 200000 + i, "stationName": f"정류소{i}", "regionName": "수원" if i < 6 else ("용인" if i < 9 else "서울")} for i in range(1, 13)]
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busRouteStationList": sts}}}
        if "getBusLocationListv2" in url:
            n = self._n("gbis_loc_" + str(p["routeId"]))
            buses = [{"plateNo": "경기70사1111", "stationSeq": min(1 + n, 60), "remainSeatCnt": 20, "crowded": 1},
                     {"plateNo": "경기70사2222", "stationSeq": min(2 + 2 * n, 60), "remainSeatCnt": 5, "crowded": 2}]
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busLocationList": buses}}}
        if "getBusArrivalListv2" in url:
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busArrivalList": [{"routeName": "7770", "predictTimeSec1": 300, "remainSeatCnt1": 12}]}}}
        if "getBusStationListv2" in url:
            return {"response": {"msgHeader": gb_ok, "msgBody": {"busStationList": [{"stationId": 1, "stationName": "사당역"}, {"stationId": 2, "stationName": "사당역(중)"}]}}}
        tg_ok = {"resultCode": "00", "resultMsg": "NORMAL SERVICE."}
        if "getRouteNoList" in url:
            city, no = int(p["cityCode"]), p["routeNo"]
            if no == "7770" and city == 31010:
                items = {"item": {"routeid": "GGB234000026", "routeno": 7770, "routetp": "직행좌석버스"}}
            elif no == "M5107" and city == 31190:
                items = {"item": {"routeid": "GGB234001243", "routeno": "M5107", "routetp": "광역급행버스"}}
            else:
                items = ""
            return {"response": {"header": tg_ok, "body": {"items": items, "totalCount": 0 if items == "" else 1}}}
        if "getRouteAcctoThrghSttnList" in url:
            its = [{"nodeid": f"GGB2000{i:02d}", "nodenm": ("신갈IC(미정차)" if i == 7 else f"정류소{i}"), "nodeord": i} for i in range(1, 15)]
            return {"response": {"header": tg_ok, "body": {"items": {"item": its}}}}
        if "getRouteAcctoBusLcList" in url:
            n = self._n("tago_loc_" + str(p["routeId"]))
            its = [{"vehicleno": "경기70사 1111", "nodeord": 1 + n // 2, "nodenm": "x"}, {"vehicleno": "경기70사3333", "nodeord": 3 + n // 2, "nodenm": "y"}]
            return {"response": {"header": tg_ok, "body": {"items": {"item": its}}}}
        if "getSttnAcctoArvlPrearngeInfoList" in url:
            return {"response": {"header": tg_ok, "body": {"items": {"item": {"routeno": 7770, "arrtime": 240}}}}}
        sl_ok = {"headerCd": "0", "headerMsg": "정상적으로 처리되었습니다.", "itemCount": 0}
        if "getBusRouteList" in url and "ws.bus.go.kr" in url:
            return {"comMsgHeader": {}, "msgHeader": sl_ok, "msgBody": {"itemList": [{"busRouteId": "100100589", "busRouteNm": "9401", "routeType": "6"}]}}
        if "getStaionByRoute" in url:
            return {"msgHeader": sl_ok, "msgBody": {"itemList": [{"arsId": "47001", "stationNm": "구미동차고지", "seq": "1"}, {"arsId": "02001", "stationNm": "서울역", "seq": "30"}]}}
        if "getBusPosByRtid" in url:
            n = self._n("seoul_pos")
            return {"msgHeader": sl_ok, "msgBody": {"itemList": [{"plainNo": "서울74사1234", "sectOrd": str(1 + n), "stopFlag": "0", "dataTm": "20261007080000"}]}}
        if "getArrInfoByRouteAll" in url:
            return {"msgHeader": sl_ok, "msgBody": {"itemList": [{"stNm": "구미동", "arrmsg1": "3분후"}]}}
        if "getStationByUid" in url:
            return {"msgHeader": sl_ok, "msgBody": {"itemList": [{"arsId": "47001", "rtNm": "9401"}]}}
        if "getUltraSrtNcst" in url:
            if p["base_time"] == "0800":  # 아직 안 올라온 상황을 흉내 낸다
                return {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
            its = [{"baseDate": p["base_date"], "baseTime": p["base_time"], "category": c, "obsrValue": v, "nx": p["nx"], "ny": p["ny"]}
                   for c, v in [("T1H", "18.2"), ("RN1", "0"), ("PTY", "0")]]
            return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"}, "body": {"items": {"item": its}}}}
        if "getRestDeInfo" in url:
            if p["solMonth"] == "10":
                items = {"item": [{"locdate": 20261003, "dateName": "개천절", "isHoliday": "Y"}, {"locdate": 20261009, "dateName": "한글날", "isHoliday": "Y"}]}
            else:
                items = ""
            return {"response": {"header": tg_ok, "body": {"items": items}}}
        if "AsosHourlyInfoService" in url:
            return ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
                    "<returnAuthMsg>SERVICE_ACCESS_DENIED_ERROR</returnAuthMsg><returnReasonCode>20</returnReasonCode>"
                    "</cmmMsgHeader></OpenAPI_ServiceResponse>")
        if "fake-ex-realtime" in url:
            t = self.clock()
            five = t.replace(minute=t.minute - t.minute % 5, second=0)
            rows = [{"routeNo": "0010", "routeName": "경부선", "conzoneId": "0010CZE305", "updownTypeCode": "E", "stdDate": t.strftime("%Y%m%d"), "stdHour": five.strftime("%H%M"), "speed": "34", "timeAvg": "162", "grade": "3"},
                    {"routeNo": "0010", "routeName": "경부선", "conzoneId": "0010CZE010", "updownTypeCode": "E", "stdDate": t.strftime("%Y%m%d"), "stdHour": five.strftime("%H%M"), "speed": "81", "timeAvg": "79", "grade": "1"},
                    {"routeNo": "0500", "routeName": "영동선", "conzoneId": "0500CZE100", "updownTypeCode": "S", "stdDate": t.strftime("%Y%m%d"), "stdHour": five.strftime("%H%M"), "speed": "90", "timeAvg": "60", "grade": "1"}]
            return {"count": len(rows), "list": rows}
        if "trafficAmountByCongest" in url:
            return {"count": 1, "list": [{"routeNo": "0010", "conzoneId": "0010CZE305", "grade": "3"}]}
        if "openapi.seoul.go.kr" in url:
            if url.rstrip("/").endswith("/1/5"):
                return {"RESULT": {"CODE": "ERROR-300", "MESSAGE": "필수 값이 누락되어 있습니다."}}
            return {"TrafficInfo": {"list_total_count": 1, "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                                    "row": [{"link_id": "1220003800", "prcs_spd": "23.0", "prcs_trv_time": "60"}]}}
        return {"error": "unknown url " + url}


def make_base(tmp: Path, cfg_patch: dict | None = None) -> tuple[Path, dict]:
    cfg = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
    cfg["road_ex"]["realtime_url"] = "https://fake-ex-realtime/openapi/x"
    cfg["seoul_traffic"]["test_link_id"] = "1220003800"
    cfg["seoul_traffic"]["link_ids"] = ["1220003800"]
    for k, v in (cfg_patch or {}).items():
        cfg[k] = v
    (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return tmp, cfg


RESULTS: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("통과  " if cond else "실패  ") + name + (f"  ({detail})" if detail else ""))


def main() -> None:
    safe_console()
    env = {"DATA_GO_KR_KEY": FAKE_KEY, "EX_KEY": FAKE_KEY, "SEOUL_KEY": FAKE_KEY}
    tmp = Path(tempfile.mkdtemp(prefix="eta_selftest_"))
    try:
        # 0) 작은 단위
        check("기상청 격자 변환: 서울시청 -> (60,127)", latlon_to_grid(37.5665, 126.9780) == (60, 127), str(latlon_to_grid(37.5665, 126.9780)))
        code, msg = api_message(Resp("u", {}, 200, "<x><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></x>", None))
        check("XML 오류 메시지 해석", code == "30" and "NOT_REGISTERED" in msg, f"{code} {msg}")
        wins = [{"days": "weekday", "start": "06:00", "end": "10:00", "every_sec": 120}, {"days": "weekend", "start": "08:00", "end": "22:00", "every_sec": 300}]
        wed = datetime(2026, 10, 7, 9, 59, tzinfo=KST)
        check("시간표: 평일 09:59는 120초", window_interval(wins, wed, False) == (120, 0))
        check("시간표: 평일 10:00은 해당 없음", window_interval(wins, wed.replace(hour=10, minute=0), False) is None)
        check("시간표: 공휴일 09:00은 주말 간격 300초", window_interval(wins, wed.replace(hour=9, minute=0), True) == (300, 0))
        t45 = datetime(2026, 10, 7, 8, 45, 3, tzinfo=KST)
        check("매시 45분 정렬", epoch_slot(t45, 3600, 2700) != epoch_slot(t45 - timedelta(seconds=10), 3600, 2700))

        # 1) API 점검 (가짜 응답)
        base, cfg = make_base(tmp)
        clock = Clock(datetime(2026, 10, 7, 7, 50, tzinfo=KST))
        http = FakeHttp(clock)
        chk = check_apis.Checker(base_dir=base, http=http, env=env, cfg=cfg, clock=clock)
        rows = chk.run()
        check_apis.print_table(rows)
        res = json.loads((base / "data/state/resolved_routes.json").read_text(encoding="utf-8"))
        check("노선 찾기: 7770 GBIS·TAGO", res["7770"].get("gbis_route_id") == "234000026" and res["7770"].get("tago_route_id") == "GGB234000026")
        check("노선 찾기: M5107은 수원에 없고 용인(31190)에서 찾음", res["M5107"].get("tago_city_code") == 31190)
        check("노선 찾기: 9401 서울 busRouteId", res["9401"].get("seoul_route_id") == "100100589")
        check("ASOS 실패가 '실패'로 표시되고 메시지를 읽음", any(r["API"].startswith("기상청 ASOS") and r["결과"].startswith("실패") and "DENIED" in r["메시지"] for r in rows))
        check("공휴일 0건 달도 성공 처리", any(r["API"] == "천문연 특일정보" and r["결과"].startswith("성공") for r in rows))
        csvp = check_apis.save_csv(rows, chk.paths.checks, clock())
        check("점검표 CSV 저장", csvp.exists())

        # 2) 수집기: 평일 07:58~09:00, 20초 간격으로 시계를 돌리고 08:20~08:30은 PC가 꺼진 것으로 흉내
        clock.t = datetime(2026, 10, 7, 7, 58, tzinfo=KST)
        col = collect.Collector(base_dir=base, http=http, clock=clock, env=env, cfg=cfg)
        col.check_startup_gap(clock())
        ticks = []
        t = clock.t
        end = datetime(2026, 10, 7, 9, 0, tzinfo=KST)
        while t <= end:
            if not (datetime(2026, 10, 7, 8, 20, 5, tzinfo=KST) <= t < datetime(2026, 10, 7, 8, 30, tzinfo=KST)):
                clock.t = t
                col.tick()
                ticks.append(t)
            t += timedelta(seconds=20)
        exp_gbis, gbis_every = expected_slots(cfg["schedules"]["bus_gbis"], ticks, False)
        exp_tago = len({int(x.timestamp()) // 60 for x in ticks})
        exp_ex = len({int(x.timestamp()) // 300 for x in ticks})
        P = Paths(base)
        day = "2026-10-07"
        recs = {j: list(read_jsonl(P.raw / j / f"{day}.jsonl")) for j in ["bus_gbis", "bus_tago", "bus_seoul", "road_ex", "weather", "seoul_link"]}
        g7770 = [r for r in recs["bus_gbis"] if r["route"] == "7770"]
        check(f"GBIS 7770 호출 수 = {gbis_every} 간격 슬롯 수(config 기준)", len(g7770) == exp_gbis, f"{len(g7770)} vs {exp_gbis}")
        t7770 = [r for r in recs["bus_tago"] if r["route"] == "7770"]
        check("TAGO 7770 호출 수 = 1분 간격 슬롯 수", len(t7770) == exp_tago, f"{len(t7770)} vs {exp_tago}")
        check("서울 9401 호출 수 = 1분 간격 슬롯 수", len(recs["bus_seoul"]) == exp_tago, f"{len(recs['bus_seoul'])} vs {exp_tago}")
        check("도로공사 호출 수 = 5분 간격 슬롯 수", len(recs["road_ex"]) == exp_ex, f"{len(recs['road_ex'])} vs {exp_ex}")
        check("도로공사 첫날은 전체 저장(full)", all(r["mode"] == "full" for r in recs["road_ex"]))
        npts = len({latlon_to_grid(p["lat"], p["lon"]) for p in cfg["weather_points"]})
        # 시작하자마자 현재 시간대(07:45~08:44) 자료를 한 번 받고, 08:45에 다음 시간대를 받는다
        exp_w = len({epoch_slot(x, 3600, 2700) for x in ticks}) * npts
        check("기상: 시작 직후 1회 + 매시 45분마다 격자 수만큼", len(recs["weather"]) == exp_w, f"{len(recs['weather'])} vs {exp_w}")
        check("기상: 08시 자료가 없으면 07시 자료로 대체", all(r["base"].endswith("0700") and r["n"] == 3 for r in recs["weather"]))
        check("서울 링크 5분 간격", len(recs["seoul_link"]) == exp_ex)
        q = json.loads((P.state / f"quota_{day}.json").read_text(encoding="utf-8"))
        gb_calls = len(recs["bus_gbis"]) + sum(1 for r in rows if r["API"] in ("경기 버스위치정보",))
        check("호출 한도 장부: GBIS 위치 호출 수 일치", q.get("gbis_buslocation") == gb_calls, f"{q.get('gbis_buslocation')} vs {gb_calls}")
        gaps = (P.logs / "gaps.csv").read_text(encoding="utf-8-sig")
        check("PC 꺼짐(약 10분) 공백 기록", "실행 중 멈춤" in gaps, gaps.strip().splitlines()[-1])
        raw_text = "".join(p.read_text(encoding="utf-8") for p in P.raw.glob("*/*.jsonl"))
        check("원본 파일에 인증키가 남지 않음", FAKE_KEY not in raw_text)
        check("공휴일 목록 저장(2026년 개천절·한글날)", "20261009" in (P.state / "holidays_2026.json").read_text(encoding="utf-8"))

        # 3) 하루 점검표
        rep = daily_report.build_report(base, day)
        (P.reports / f"daily_{day}.md").write_text(rep, encoding="utf-8")
        check("점검표: GBIS-TAGO 비교 표 생성", "GBIS와 TAGO 비교" in rep)
        check("점검표: 도로공사 노선 목록(경부선) 표시", "0010 경부선" in rep)
        check("점검표: 공백 표시", "실행 중 멈춤" in rep)

        # 4) 다음 날: 지난 파일 압축, 도로공사 필터·중복 제거, 공휴일은 주말 시간표
        cfg2 = dict(cfg)
        cfg2["road_ex"] = dict(cfg["road_ex"], full_dump_days=1, keep_route_nos=["0010"])
        clock.t = datetime(2026, 10, 9, 8, 0, 1, tzinfo=KST)  # 한글날(금) = 공휴일
        hol_ticks = [clock.t, datetime(2026, 10, 9, 8, 0, 50, tzinfo=KST)]  # 아래에서 tick()을 두 번 부른 시각
        # 가짜 API가 노선 ID를 돌려주는 노선만 수집된다(M4101은 가짜 API에 없음) → 평일에 실제로 부른 노선 수를 기준으로
        n_gbis_routes = len({r["route"] for r in recs["bus_gbis"]})
        hol_slots, hol_every = expected_slots(cfg["schedules"]["bus_gbis"], hol_ticks, True)
        col2 = collect.Collector(base_dir=base, http=http, clock=clock, env=env, cfg=cfg2)
        col2.tick()
        check("지난 날짜 원본 .gz 압축", (P.raw / "bus_gbis" / f"{day}.jsonl.gz").exists() and not (P.raw / "bus_gbis" / f"{day}.jsonl").exists())
        ex2 = list(read_jsonl(P.raw / "road_ex" / "2026-10-09.jsonl"))
        check("도로공사 필터: 경부선(0010) 2행만 저장", ex2 and ex2[-1]["mode"] == "filtered" and ex2[-1]["n_new"] == 2, str(ex2[-1].get("n_new") if ex2 else None))
        clock.t = datetime(2026, 10, 9, 8, 0, 50, tzinfo=KST)
        col2.last_slot.pop("road_ex", None)  # 같은 5분 안에 한 번 더 호출된 상황
        col2.tick()
        ex2 = list(read_jsonl(P.raw / "road_ex" / "2026-10-09.jsonl"))
        check("도로공사 중복 제거: 같은 stdHour는 다시 저장하지 않음", ex2[-1]["n_new"] == 0)
        g9 = list(read_jsonl(P.raw / "bus_gbis" / "2026-10-09.jsonl"))
        check(f"공휴일 08:00은 주말 시간표({hol_every})로 GBIS 호출(노선 {n_gbis_routes}개)",
              len(g9) == n_gbis_routes * hol_slots, f"{len(g9)} vs {n_gbis_routes * hol_slots}")
        # 호출 수만으로는 평일 시간표와 구분이 안 될 수 있어(08:00:01·08:00:50은 1분 간격에서도 같은 칸) 실제로 쓴 간격도 본다
        hol_iv = window_interval(cfg["schedules"]["bus_gbis"], hol_ticks[0], True)
        used = {col2.last_slot[j.id][0] for j in col2.jobs if j.schedule == "bus_gbis" and j.id in col2.last_slot}
        check("공휴일 GBIS 호출 간격이 config 주말 간격과 같음", hol_iv is not None and used == {hol_iv[0]},
              f"{sorted(used)} vs {hol_iv[0] if hol_iv else None}")

        # 5) 한도 초과 시 멈춤
        cfg3 = dict(cfg)
        cfg3["quota"] = {"safety_ratio": 1.0, "limits": dict(cfg["quota"]["limits"], seoul_buspos=3)}
        base3 = Path(tempfile.mkdtemp(prefix="eta_selftest_q_"))
        make_base(base3)
        shutil.copy(P.state / "resolved_routes.json", Paths(base3).state / "resolved_routes.json")
        clock.t = datetime(2026, 10, 7, 7, 0, tzinfo=KST)
        col3 = collect.Collector(base_dir=base3, http=http, clock=clock, env=env, cfg=cfg3)
        for i in range(10):
            clock.t = datetime(2026, 10, 7, 7, i, 1, tzinfo=KST)
            col3.tick()
        s3 = list(read_jsonl(Paths(base3).raw / "bus_seoul" / "2026-10-07.jsonl"))
        check("한도 3회로 줄이면 서울 위치는 3회에서 멈춤", len(s3) == 3, str(len(s3)))
        shutil.rmtree(base3, ignore_errors=True)
    finally:
        print()
        ok = sum(1 for _, c, _ in RESULTS if c)
        print(f"결과: {ok}/{len(RESULTS)} 통과")
        print("전체 통과" if ok == len(RESULTS) else "실패한 항목이 있습니다. 위 '실패' 줄을 확인하세요.")
        print(f"(임시 폴더: {tmp})")


if __name__ == "__main__":
    main()
