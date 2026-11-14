"""3단계: 하루 수집 결과 점검표. 파일럿 기간에 매일 한 번 실행한다.

실행:  python daily_report.py                (오늘)
       python daily_report.py --date 2026-10-06

결과: 화면 출력 + data/reports/daily_YYYY-MM-DD.md

보는 것
  1. 작업별 호출 수와 성공률
  2. 버스: 노선·출처별로 잡힌 차량 수, 정류소 순번이 바뀐 횟수, 순번이 2칸 이상 건너뛴 비율
     → 건너뛴 비율이 높으면 호출 간격을 줄여야 한다
  3. GBIS와 TAGO가 같은 시각에 같은 차량을 보여 주는지 (차량 일치율)
  4. 도로공사: 갱신 주기(stdHour가 바뀌는 간격), 노선번호-노선명 목록 (keep_route_nos 고를 때 사용)
  5. 수집 공백, 오늘 호출 수
  6. 빈자리(만석): 승차 정류장마다 버스가 빈자리 0으로 도착한 비율, 0으로 떠난 비율, 만석이 된 운행 비율,
     차량 정원, 판정에서 빠진 통과의 이유, 중간 출발 후보
     (2026-11-11 추가. data/ref의 정류소 목록이 필요하다 → check_apis.py를 한 번 실행해 두면 생긴다)
     2026-11-11 보완: 서울 isFullFlag=1을 만석으로 처리, 퇴근용 '만석 출발' 열, 정원은 차량번호별 최대값,
     시간대 표기를 06:00~09:59 / 17:00~21:59로, TAGO 결과 코드 99를 실패로 셈, TAGO 순번 지연 비율
     2026-11-13 보완: 출근 수집을 05:30부터 하므로, 시간대 시작 30분 전 기록을 '앞 정류장' 판정에 씀
     (집계는 06:00~09:59에 지난 정류장만). 05:30 전 기록이 없는 날은 결과가 이전과 같다
     2026-11-14 보완: 토·일·공휴일은 출근/퇴근 대신 방향(서울행·귀가)별로 06:00~21:59 전체와 2시간 구간을 집계
     (공휴일은 collect.py가 저장한 data/state/holidays_YYYY.json 기준). 평일 결과는 이전과 같다
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from common import safe_console, BASE_DIR, Paths, now_kst, read_json, read_jsonl, is_offday

BUS_FIELDS = {  # 출처별 (차량번호 필드, 정류소 순번 필드)
    "bus_gbis": ("plateNo", "stationSeq"),
    "bus_tago": ("vehicleno", "nodeord"),
    "bus_seoul": ("plainNo", "sectOrd"),
}


def day_files(raw: Path, day: str) -> dict[str, Path]:
    out = {}
    for d in sorted(p for p in raw.iterdir() if p.is_dir()):
        for cand in (d / f"{day}.jsonl", d / f"{day}.jsonl.gz"):
            if cand.exists():
                out[d.name] = cand
                break
    return out


def norm_plate(x) -> str:
    return str(x or "").replace(" ", "").strip()


OK_CODES = {None, "", "0", "00", "INFO-000"}  # HTTP 200이어도 이 밖의 결과 코드는 실패로 센다(TAGO는 99)


def code_ok(r: dict) -> bool:
    return str(r.get("code")) in OK_CODES or r.get("code") is None


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def bus_section(job: str, recs: list[dict]) -> tuple[list[str], dict]:
    veh_f, seq_f = BUS_FIELDS[job]
    lines = []
    by_route = defaultdict(list)
    for r in recs:
        by_route[r.get("route", "?")].append(r)
    slot_sets: dict = {}
    for route, rs in sorted(by_route.items()):
        rs.sort(key=lambda r: r["ts"])
        polls = len(rs)
        with_bus = sum(1 for r in rs if r.get("n", 0) > 0)
        last_seq: dict = {}
        changes = jumps = backs = 0
        vehicles = set()
        sets = {}
        for r in rs:
            minute = r["ts"][:16]
            cur = {}  # 차량 → 정류소 순번
            for it in r.get("items") or []:
                v = norm_plate(it.get(veh_f))
                s = to_int(it.get(seq_f))
                if not v or s is None:
                    continue
                vehicles.add(v)
                cur[v] = s
                if v in last_seq and s != last_seq[v]:
                    d = s - last_seq[v]
                    if d > 0:
                        changes += 1
                        if d >= 2:
                            jumps += 1
                    else:
                        backs += 1
                last_seq[v] = s
            sets[minute] = cur
        slot_sets[route] = sets
        jump_ratio = f"{jumps / changes * 100:.1f}%" if changes else "-"
        lines.append(f"| {route} | {polls} | {with_bus} | {len(vehicles)} | {changes} | {jump_ratio} | {backs} |")
    return lines, slot_sets


def ex_section(recs: list[dict]) -> list[str]:
    lines = []
    calls = len(recs)
    ok = sum(1 for r in recs if r.get("http") == 200)
    totals = [r.get("n_total", 0) for r in recs]
    lines.append(f"- 호출 {calls}회, HTTP 200 {ok}회, 1회 응답 행 수 중앙값 {statistics.median(totals) if totals else 0}")
    hours = sorted({str(it.get("stdHour")) for r in recs for it in (r.get("items") or []) if it.get("stdHour")})
    if hours:
        mins = [int(h[:2]) * 60 + int(h[2:]) for h in hours if len(h) == 4 and h.isdigit()]
        gaps = [b - a for a, b in zip(mins, mins[1:]) if b > a]
        med = statistics.median(gaps) if gaps else None
        lines.append(f"- 서로 다른 stdHour {len(hours)}개 (예: {', '.join(hours[:6])} ...), 바뀌는 간격 중앙값 {med}분"
                     " → 수집 간격(road_ex every_sec)을 이 값에 맞추면 중복 호출이 줄어든다")
    routes = Counter()
    for r in recs:
        if r.get("mode") == "full":
            for it in r.get("items") or []:
                routes[(str(it.get("routeNo")), str(it.get("routeName")))] += 1
    if routes:
        lines.append("- 노선번호-노선명 (전체 저장 기간에만 표시, keep_route_nos 고를 때 참고):")
        for (no, nm), c in sorted(routes.items()):
            lines.append(f"  - {no} {nm} ({c}행)")
    modes = Counter(r.get("mode") for r in recs)
    lines.append(f"- 저장 방식: {dict(modes)}")
    return lines


# ---------------------------------------------------------------------------
# 8. 빈자리(만석) 통계
#
# 정류장 k를 지나는 운행 1회마다 두 가지를 판정한다.
#   도착: k보다 앞에서 마지막으로 본 빈자리가 0 → 만석 도착(확실)
#         k 이후 처음 본 빈자리가 1 이상          → 자리 있음(확실). 승차 구간에서는 빈자리가 줄기만 하므로
#         둘 다 아니면                              → 애매
#   출발: 만석 도착이었거나, k~다음 승차 정류장 사이에서 빈자리 0을 봤으면 → 만석 출발(확실)
#         k를 지난 뒤(순번 > k) 빈자리 1 이상을 봤으면                       → 자리 남기고 출발(확실)
#         둘 다 아니면                                                       → 애매
#   만석 출발 − 만석 도착 = 이 정류장에서 찬 버스. 줄이 남았을 수 있다(퇴근은 대부분 여기에 잡힌다).
# 출근 = 06:00~09:59, 회차 전·서울 밖 승차 정류장 / 퇴근 = 17:00~21:59, 회차 후·서울 안 승차 정류장.
# 네 노선 모두 경기에서 출발해 서울에서 회차하는 구조를 전제로 한다.
# ---------------------------------------------------------------------------
SEAT_SOURCES = {  # 출처: (빈자리 필드, 정류소 순번 필드, 차량 필드)
    "bus_gbis": ("remainSeatCnt", "stationSeq", "plateNo"),
    "bus_seoul": ("congetion", "sectOrd", "plainNo"),
}
PERIODS = [("출근", 6, 10), ("퇴근", 17, 22)]  # 시작 시 이상, 끝 시 미만
# 토·일·공휴일: 출퇴근 구분 대신 방향별 하루 전체(06:00~21:59) + 2시간 구간 표 (2026-11-14 주말 지침)
# 방향은 평일과 같은 승차 정류장을 쓴다: 서울행 = 회차 전·서울 밖(평일 출근 쪽), 귀가 = 회차 후·서울 안(평일 퇴근 쪽)
OFFDAY_PERIODS = [("서울행", 6, 22), ("귀가", 6, 22)]
OFFDAY_BIN_H = 2
INBOUND = {"출근", "서울행"}
NOSTOP_WORDS = ("미정차", "(경유)", "(가상)")
STALE_SEC = 180          # 서울 위치 자료(dataTm)가 이보다 오래되면 버린다
NEW_TRIP_GAP_SEC = 1800  # 같은 차량이라도 이만큼 안 보이면 새 운행으로 본다
EDGE_SEC = 300           # 시간대 첫/마지막 호출에서 이 안쪽이면 '시간대 경계에 걸린 운행'으로 본다
LEAD_MIN = 30            # 시간대 시작 전 이만큼의 기록은 '앞 정류장' 판정에만 쓴다(2026-11-13 출근 수집 05:30 시작)
MAX_ROWS = 8             # 노선·시간대마다 표에 보일 정류장 수


def load_route_ref(ref_dir: Path, source: str, route: str):
    """정류소 목록 → (회차 순번, {순번: (이름, 서울 여부)}, 통과 지점 순번 집합). 없으면 None."""
    if source == "bus_gbis":
        st = read_json(ref_dir / f"gbis_route_stations_{route}.json", None)
        if not st:
            return None
        turn = to_int(st[0].get("turnSeq"))
        stations = {to_int(s["stationSeq"]): (s.get("stationName", ""), s.get("regionName") == "서울") for s in st}
        nostop = {k for k, (n, _) in stations.items() if any(w in n for w in NOSTOP_WORDS)}
    else:
        st = read_json(ref_dir / f"seoul_route_stations_{route}.json", None)
        if not st:
            return None
        st = sorted(st, key=lambda s: to_int(s.get("seq")) or 0)
        first_dir = st[0].get("direction")
        turn = max(to_int(s["seq"]) for s in st if s.get("direction") == first_dir)
        # 서울시 정류소 ID는 1로 시작한다(경기 구간은 2로 시작)
        stations = {to_int(s["seq"]): (s.get("stationNm", ""), str(s.get("station", "")).startswith("1")) for s in st}
        nostop = {to_int(s["seq"]) for s in st if s.get("stationNo") == "미정차"
                  or any(w in s.get("stationNm", "") for w in NOSTOP_WORDS)}
    if turn is None:
        return None
    return turn, stations, nostop


def seat_trips(source: str, recs: list[dict]):
    """노선별 운행 목록(운행 = [(시각, 순번, 빈자리), ...]), 노선별 차량 최대 빈자리, 노선별 호출 시각."""
    seat_f, seq_f, veh_f = SEAT_SOURCES[source]
    obs = defaultdict(list)
    vcap: dict = defaultdict(dict)
    polls: dict = defaultdict(list)
    for r in recs:
        if r.get("http") != 200:
            continue
        t = datetime.fromisoformat(r["ts"])
        polls[r.get("route", "?")].append(t)
        for it in r.get("items") or []:
            seat, seq, veh = to_int(it.get(seat_f)), to_int(it.get(seq_f)), norm_plate(it.get(veh_f))
            if seat is None or seq is None or not veh or seat < 0:
                continue
            if source == "bus_seoul":
                if seq == 1 and seat == 0:  # 차고지 대기 차량은 0을 보낸다
                    continue
                try:
                    dt = datetime.strptime(str(it.get("dataTm")), "%Y%m%d%H%M%S").replace(tzinfo=t.tzinfo)
                    if (t - dt).total_seconds() > STALE_SEC:
                        continue
                except ValueError:
                    pass
                # 만석이 몇 분 이어지면 서울 API는 congetion을 0 대신 정원값(9401은 41)으로 보내고
                # isFullFlag=1을 붙인다(2026-11-11 원본 확인). 이 경우는 빈자리 0으로 바꾼다.
                if str(it.get("isFullFlag")) == "1":
                    seat = 0
                else:
                    route_caps = vcap[r.get("route", "?")]
                    route_caps[veh] = max(route_caps.get(veh, 0), seat)
            else:
                route_caps = vcap[r.get("route", "?")]
                route_caps[veh] = max(route_caps.get(veh, 0), seat)
            obs[(r.get("route", "?"), veh)].append((t, seq, seat))
    trips = defaultdict(list)
    for (route, _veh), L in obs.items():
        L.sort()
        cur: list = []
        for o in L:
            if cur and (o[1] < cur[-1][1] - 1 or (o[0] - cur[-1][0]).total_seconds() > NEW_TRIP_GAP_SEC):
                trips[route].append(cur)
                cur = []
            cur.append(o)
        if cur:
            trips[route].append(cur)
    return trips, vcap, polls


def max_run(events: list, key: str) -> int:
    run = mx = 0
    for _, c in sorted(events):
        run = run + 1 if c == key else 0
        mx = max(mx, run)
    return mx


def seat_route_lines(route: str, trips: list, caps: dict, polls: list, ref, day: str,
                     offday: bool = False) -> list[str]:
    turn, stations, nostop = ref
    lines = []
    cap_cnt = Counter(caps.values())
    lines.append(f"**{route}** — 차량 {len(caps)}대의 오늘 최대 빈자리(정원 추정): "
                 + ", ".join(f"{c}석 {n}대" for c, n in sorted(cap_cnt.items(), key=lambda x: (-x[1], -x[0]))[:4])
                 + "  ← 하루 동안 한 번도 빈 차로 잡히지 않은 차량은 실제 정원보다 작게 나온다")
    for label, h0, h1 in (OFFDAY_PERIODS if offday else PERIODS):
        if label in INBOUND:
            stops = sorted(k for k, (_, seoul) in stations.items() if k < turn and not seoul and k not in nostop)
        else:
            stops = sorted(k for k, (_, seoul) in stations.items() if k >= turn and seoul and k not in nostop)
        stops = [k for k in stops if any(j < k for j in stations)]  # 노선 기점은 '도착'이 없으므로 뺀다
        if not stops:
            continue
        lo, hi = stops[0], stops[-1]
        nxt = min([k for k in stations if k > hi and k not in nostop] or [hi + 1])
        next_stop = {k: (stops[i + 1] if i + 1 < len(stops) else nxt) for i, k in enumerate(stops)}
        def judge(h0, h1):  # 시간대 [h0시, h1시) 안의 통과를 판정한다. 호출·운행이 없으면 None
            def in_win(t, lead=0):  # 시간대 안(lead분 앞당김 허용)
                m = t.hour * 60 + t.minute
                return t.strftime("%Y-%m-%d") == day and h0 * 60 - lead <= m < h1 * 60
            win = [t for t in polls if in_win(t, LEAD_MIN)]
            if not any(in_win(t) for t in win):
                return None
            w0, w1 = min(win), max(win)
            stat = {k: Counter() for k in stops}
            ev_arr = defaultdict(list)
            ev_dep = defaultdict(list)
            miss = Counter()
            first_seen = Counter()
            n_trip = n_full = 0
            for L in trips:
                seg = [o for o in L if lo - 1 <= o[1] < nxt and in_win(o[0], LEAD_MIN)]
                if len(seg) < 2 or not any(in_win(o[0]) for o in seg):
                    continue
                n_trip += 1
                if any(o[1] >= lo and o[2] == 0 and in_win(o[0]) for o in seg):
                    n_full += 1
                at_start = (seg[0][0] - w0).total_seconds() <= EDGE_SEC
                at_end = (w1 - seg[-1][0]).total_seconds() <= EDGE_SEC
                if not at_start and seg[0][1] > stops[min(1, len(stops) - 1)]:
                    k0 = max([x for x in stops if x <= seg[0][1]] or [seg[0][1]])
                    first_seen[stations.get(k0, ("?",))[0]] += 1
                for k in stops:
                    before = [o for o in seg if o[1] < k]
                    after = [o for o in seg if o[1] >= k]
                    if not before:
                        miss["시간대 시작 때 이미 지남" if at_start else "이 정류장 뒤에서 처음 잡힘"] += 1
                        continue
                    if not after:
                        miss["시간대 끝까지 못 옴" if at_end else "이 정류장 전에 사라짐"] += 1
                        continue
                    if not in_win(after[0][0]):  # 시간대 시작 전에 지난 정류장은 집계하지 않는다
                        miss["시간대 시작 전에 통과(집계 대상 아님)"] += 1
                        continue
                    if before[-1][2] == 0:
                        arr, dep = "full", "full"
                    else:
                        arr = "ok" if after[0][2] > 0 else "amb"
                        if any(o[2] == 0 for o in after if o[1] < next_stop[k]):
                            dep = "full"
                        elif any(o[2] > 0 for o in after if o[1] > k):
                            dep = "ok"
                        else:
                            dep = "amb"
                    stat[k]["arr_" + arr] += 1
                    stat[k]["dep_" + dep] += 1
                    ev_arr[k].append((after[0][0], arr))
                    ev_dep[k].append((after[0][0], dep))
            if n_trip == 0:
                return None
            passes = sum(stat[k]["arr_full"] + stat[k]["arr_ok"] + stat[k]["arr_amb"] for k in stops)
            amb_a = sum(stat[k]["arr_amb"] for k in stops)
            amb_d = sum(stat[k]["dep_amb"] for k in stops)
            expected = n_trip * len(stops)
            return dict(n_trip=n_trip, n_full=n_full, stat=stat, ev_arr=ev_arr, ev_dep=ev_dep, miss=miss,
                        first_seen=first_seen, passes=passes, amb_a=amb_a, amb_d=amb_d, expected=expected)

        r = judge(h0, h1)
        if r is None:
            continue
        n_trip, n_full, stat, ev_arr, ev_dep, miss = (r[k] for k in ("n_trip", "n_full", "stat", "ev_arr", "ev_dep", "miss"))
        first_seen, passes, amb_a, amb_d, expected = (r[k] for k in ("first_seen", "passes", "amb_a", "amb_d", "expected"))
        lines.append("")
        lines.append(f"- {label}({h0:02d}:00~{h1 - 1:02d}:59): 운행 {n_trip}회, 승차 구간에서 만석이 된 운행 {n_full}회"
                     f" ({n_full / n_trip * 100:.0f}%), 판정 애매 도착 {amb_a}/{passes} ({amb_a / passes * 100 if passes else 0:.1f}%)"
                     f" · 출발 {amb_d}/{passes} ({amb_d / passes * 100 if passes else 0:.1f}%)")
        if expected > passes:
            lines.append(f"  - 판정한 통과 {passes}/{expected} (운행 × 승차 정류장). 빠진 이유: "
                         + ", ".join(f"{k} {v}" for k, v in miss.most_common()))
        if first_seen:
            lines.append("  - 시간대 중간에 노선 앞부분을 건너뛰고 처음 잡힌 운행(중간 출발 후보): "
                         + ", ".join(f"{nm} {c}회" for nm, c in first_seen.most_common(3)))
        if offday:  # 주말·공휴일: 2시간 구간별로 다시 판정한다
            lines += ["", "  | 시간 | 운행 | 만석이 된 운행 | 판정 애매(도착 · 출발) | 만석 출발이 가장 많은 정류장 |",
                      "  | --- | --- | --- | --- | --- |"]
            for b0 in range(h0, h1, OFFDAY_BIN_H):
                b1 = min(b0 + OFFDAY_BIN_H, h1)
                b = judge(b0, b1)
                span = f"{b0:02d}:00~{b1 - 1:02d}:59"
                if b is None:
                    got = any(t.strftime("%Y-%m-%d") == day and b0 * 60 <= t.hour * 60 + t.minute < b1 * 60 for t in polls)
                    lines.append(f"  | {span} | {'0' if got else '수집 없음'} | - | - | - |")
                    continue
                bs = b["stat"]
                best = max(stops, key=lambda k: (bs[k]["dep_full"], -k))
                bn = bs[best]["arr_full"] + bs[best]["arr_ok"] + bs[best]["arr_amb"]
                top_txt = f"{stations[best][0]} {bs[best]['dep_full']}/{bn}" if bs[best]["dep_full"] else "-"
                bp = b["passes"]
                lines.append(f"  | {span} | {b['n_trip']} | {b['n_full']} ({b['n_full'] / b['n_trip'] * 100:.0f}%)"
                             f" | {b['amb_a'] / bp * 100 if bp else 0:.1f}% · {b['amb_d'] / bp * 100 if bp else 0:.1f}%"
                             f" | {top_txt} |")
            lines.append(f"  - 구간 경계({OFFDAY_BIN_H}시간)에 걸친 운행은 양쪽 구간에 모두 센다."
                         " 아래 정류장 표는 하루 전체 기준")
        rows = []
        for k in stops:
            s = stat[k]
            n = s["arr_full"] + s["arr_ok"] + s["arr_amb"]
            if n == 0 or s["dep_full"] == 0:
                continue
            rows.append((s["dep_full"] / n, k, n, s["arr_full"], s["dep_full"], s["dep_amb"],
                         max_run(ev_arr[k], "full"), max_run(ev_dep[k], "full")))
        top = sorted(rows, key=lambda r: (-r[0], r[1]))[:MAX_ROWS]
        if top:
            lines += ["", "  | 정류장 | 통과 | 만석 도착 | 만석 출발 | 출발 애매 | 연속 만석 최대(도착/출발) |",
                      "  | --- | --- | --- | --- | --- | --- |"]
            for ratio, k, n, fa, fd, ad, ra, rd in sorted(top, key=lambda r: r[1]):
                lines.append(f"  | {stations[k][0]} | {n} | {fa} ({fa / n * 100:.0f}%) | {fd} ({ratio * 100:.0f}%)"
                             f" | {ad} | {ra}대 / {rd}대 |")
            if len(rows) > MAX_ROWS:
                lines.append(f"  - 만석 출발이 있는 정류장 {len(rows)}곳 중 비율이 높은 {MAX_ROWS}곳만 보임")
        else:
            lines.append("  - 만석으로 떠난 버스 없음")
    return lines


def load_holidays(paths: Paths, day: str) -> set:
    """collect.py가 받아 둔 공휴일 목록(YYYYMMDD). 파일이 없으면 빈 집합 → 토·일만 주말로 본다."""
    return set(read_json(paths.state / f"holidays_{day[:4]}.json", None) or [])


def seat_section(paths: Paths, all_recs: dict, day: str, offday: bool = False) -> list[str]:
    out = ["## 8. 빈자리(만석)", "",
           "승차 정류장마다 버스가 빈자리 0으로 도착한 비율(만석 도착)과 0으로 떠난 비율(만석 출발, 만석 도착 포함). "
           "만석 출발이 만석 도착보다 많은 정류장은 그 정류장에서 버스가 찬 것으로, 줄이 남았을 수 있다. "
           "같은 노선에서 기점에서 멀수록 높아지면 승차 기회 격차다. "
           "'판정 애매'는 호출 사이(1분)에 버스가 찬 경우로, 몇 % 안 되면 호출 간격은 충분하다.", ""]
    if offday:
        out += ["주말·공휴일이라 출근/퇴근 대신 방향별로 본다. 서울행 = 평일 출근과 같은 승차 정류장(회차 전·서울 밖), "
                "귀가 = 평일 퇴근과 같은 승차 정류장(회차 후·서울 안). 하루 전체(06:00~21:59) 요약 뒤에 2시간 구간 표를 붙인다. "
                "주말 호출 간격(2026-11-14 지침부터 2분)이 평일 1분보다 길어 '판정 애매'가 평일보다 높게 나올 수 있다.", ""]
    any_route = False
    for source in ("bus_gbis", "bus_seoul"):
        if source not in all_recs:
            continue
        trips_by_route, vcap, polls = seat_trips(source, all_recs[source])
        for route in sorted(trips_by_route):
            ref = load_route_ref(paths.ref, source, route)
            if ref is None:
                out.append(f"- {route}: data/ref에 정류소 목록이 없어 건너뜀 (check_apis.py를 실행하면 생긴다)")
                continue
            out += seat_route_lines(route, trips_by_route[route], vcap[route], polls[route], ref, day, offday) + [""]
            any_route = True
    if not any_route:
        out.append("- 빈자리 자료 없음")
    return out


def build_report(base_dir: Path, day: str) -> str:
    paths = Paths(base_dir)
    files = day_files(paths.raw, day)
    offday = is_offday(datetime.strptime(day, "%Y-%m-%d"), load_holidays(paths, day))
    out = [f"# 수집 점검 {day}" + (" (주말·공휴일)" if offday else ""), ""]
    if not files:
        out.append("이 날짜의 원본 파일이 없습니다.")
        return "\n".join(out)

    out += ["## 1. 작업별 호출", "", "| 작업 | 호출 | HTTP 200 | 데이터 1건 이상 | 응답시간 중앙값(ms) |", "| --- | --- | --- | --- | --- |"]
    all_recs = {}
    for job, p in files.items():
        recs = list(read_jsonl(p))
        all_recs[job] = recs
        by_key = defaultdict(list)
        for r in recs:
            by_key[r.get("route") or r.get("point") or r.get("link_id") or job].append(r)
        for k, rs in sorted(by_key.items()):
            ok = sum(1 for r in rs if r.get("http") == 200)
            bad = sum(1 for r in rs if r.get("http") == 200 and not code_ok(r))
            nz = sum(1 for r in rs if r.get("n", 0) > 0)
            ms = statistics.median([r.get("ms", 0) for r in rs]) if rs else 0
            label = job if k == job else f"{job}:{k}"
            note = f" (오류 코드 {bad})" if bad else ""
            out.append(f"| {label} | {len(rs)} | {ok} ({ok / len(rs) * 100:.0f}%){note} | {nz} | {ms:.0f} |")
        fails = Counter((r.get("code"), (r.get("msg") or "")[:60]) for r in recs
                        if r.get("http") != 200 or r.get("msg") or not code_ok(r))
        for (code, msg), c in fails.most_common(3):
            hours = Counter(r["ts"][11:13] for r in recs if r.get("code") == code and (r.get("msg") or "")[:60] == msg)
            when = ", ".join(f"{h}시 {n}" for h, n in sorted(hours.items()))
            out.append(f"| (실패 예) {job} | {c} | code={code} | {msg} | {when} |")
    out.append("")

    out += ["## 2. 버스 위치", "",
            "순번 변화 = 차량의 정류소 순번이 앞으로 바뀐 횟수(= 정류장 통과를 잡은 횟수). "
            "2칸 이상 건너뜀 비율이 높으면 그 사이 정류장 통과 시각을 놓친 것이다. 역행은 회차나 새 운행 시작.", "",
            "| 노선 | 호출 | 버스 잡힌 호출 | 차량 수 | 순번 변화 | 2칸 이상 건너뜀 | 역행 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    sets_by_src = {}
    for job in ("bus_gbis", "bus_tago", "bus_seoul"):
        if job in all_recs:
            out.append(f"| **{job}** | | | | | | |")
            lines, sets = bus_section(job, all_recs[job])
            out += lines
            sets_by_src[job] = sets
    out.append("")

    if "bus_gbis" in sets_by_src and "bus_tago" in sets_by_src:
        out += ["## 3. GBIS와 TAGO 비교 (같은 분에 둘 다 받은 경우)", "",
                "| 노선 | 비교한 시각 수 | 차량 일치율 평균 | TAGO에만 있는 차량 비율 | TAGO 순번이 GBIS보다 뒤처진 비율 |",
                "| --- | --- | --- | --- | --- |"]
        g, t = sets_by_src["bus_gbis"], sets_by_src["bus_tago"]
        for route in sorted(set(g) & set(t)):
            common = sorted(set(g[route]) & set(t[route]))
            jac, only_t = [], []
            same = behind = 0
            for m in common:
                a, b = g[route][m], t[route][m]
                ka, kb = set(a), set(b)
                if ka or kb:
                    jac.append(len(ka & kb) / len(ka | kb))
                    only_t.append(len(kb - ka) / len(kb) if kb else 0)
                for v in ka & kb:
                    d = a[v] - b[v]
                    same += d == 0
                    behind += 0 < d <= 3
            lag = f"{behind / (same + behind) * 100:.0f}%" if same + behind else "-"
            if jac:
                out.append(f"| {route} | {len(jac)} | {statistics.mean(jac) * 100:.0f}% | {statistics.mean(only_t) * 100:.0f}% | {lag} |")
            else:
                out.append(f"| {route} | 0 | - | - | - |")
        out += ["", "TAGO 위치 응답에는 잔여좌석이 없다. 빈자리 수집은 GBIS가 맡고, 일치율이 90% 이상으로 꾸준하면"
                " TAGO를 GBIS 장애 때의 위치 백업으로 쓸 수 있다.", ""]

    if "road_ex" in all_recs:
        out += ["## 4. 도로공사 실시간 소통", ""] + ex_section(all_recs["road_ex"]) + [""]

    if "weather" in all_recs:
        cats = Counter(it.get("category") for r in all_recs["weather"] for it in (r.get("items") or []))
        out += ["## 5. 기상 (초단기실황)", "", f"- 받은 항목: {dict(cats)}", ""]

    out += ["## 6. 수집 공백", ""]
    gp = paths.logs / "gaps.csv"
    gaps = []
    if gp.exists():
        with open(gp, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("start", "").startswith(day) or row.get("end", "").startswith(day):
                    gaps.append(row)
    if gaps:
        for gp_ in gaps:
            out.append(f"- {gp_['start']} ~ {gp_['end']} ({gp_['minutes']}분, {gp_['reason']})")
    else:
        out.append("- 없음")
    out.append("")
    q = read_json(paths.state / f"quota_{day}.json", {})
    out += ["## 7. 오늘 호출 수 (한도 묶음별)", "", f"- {q}", ""]
    try:
        out += seat_section(paths, all_recs, day, offday)
    except Exception as e:  # 빈자리 통계가 실패해도 나머지 점검표는 저장한다
        out += ["## 8. 빈자리(만석)", "", f"- 계산 중 오류로 건너뜀: {type(e).__name__}: {e}", ""]
    return "\n".join(out)


def main() -> None:
    safe_console()
    ap = argparse.ArgumentParser(description="하루 수집 결과 점검표")
    ap.add_argument("--date", default=now_kst().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    text = build_report(BASE_DIR, args.date)
    print(text)
    p = Paths(BASE_DIR).reports / f"daily_{args.date}.md"
    p.write_text(text, encoding="utf-8")
    print(f"\n저장: {p}")


if __name__ == "__main__":
    main()
