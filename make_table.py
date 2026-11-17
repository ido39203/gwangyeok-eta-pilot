"""'운행 1회 × 정류장' 표 만들기 (2026-11-17, v0).

수집 원본(data/raw)을 하루 단위로 읽어, 버스 한 번의 운행이 승차 정류장 하나를 지날 때마다 한 줄을 쓴다.
판정(만석 도착·만석 출발·애매)은 daily_report.py와 같은 함수를 그대로 쓰므로 일일 점검표의 숫자와 맞는다.
열 설명은 docs/TABLE.md.

사용:
  python make_table.py                      어제까지의 날짜 중 표가 없거나 원본이 더 새로운 날만 만들기
  python make_table.py --date 2026-11-17    그 날만 다시 만들기
  python make_table.py --from 2026-11-12 --to 2026-11-17
  python make_table.py --rebuild            원본이 있는 모든 날을 다시 만들기

결과:
  data/tables/passes_YYYY-MM-DD.csv   날짜별 표
  data/tables/passes_all.csv          날짜별 표를 이어 붙인 전체 표(매번 다시 만든다)
엑셀에서 한글이 깨지지 않도록 UTF-8(BOM)으로 저장한다. pandas는 encoding="utf-8-sig"로 읽는다.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

import daily_report as dr
from common import BASE_DIR, Paths, is_offday, now_kst, read_jsonl, safe_console

TABLE_VERSION = "v0"
WEEKDAY_KO = "월화수목금토일"
COLUMNS = [
    "date", "weekday", "day_type", "special_note", "source", "route", "period",
    "trip_id", "vehicle", "capacity_est", "mid_start", "trip_first_seq", "trip_first_time",
    "stop_seq", "stop_name", "stop_order", "n_stops",
    "pass_time", "pass_min", "arr_seats", "dep_seats", "arr", "dep", "full_dep",
]
BUS_SOURCES = ("bus_gbis", "bus_seoul")


def day_rows(base_dir: Path, day: str) -> list[dict]:
    """하루치 표의 줄 목록. 원본이 없으면 빈 목록."""
    paths = Paths(base_dir)
    files = dr.day_files(paths.raw, day)
    offday = is_offday(datetime.strptime(day, "%Y-%m-%d"), dr.load_holidays(paths, day))
    special = dr.load_special_days(base_dir).get(day, "")
    day_type = "특일" if special else ("주말·공휴일" if offday else "평일")
    weekday = WEEKDAY_KO[datetime.strptime(day, "%Y-%m-%d").weekday()]
    out = []
    for source in BUS_SOURCES:
        if source not in files:
            continue
        recs = list(read_jsonl(files[source]))
        trips, vcap, polls, trip_veh = dr.seat_trips(source, recs, with_vehicle=True)
        for route in sorted(trips):
            ref = dr.load_route_ref(paths.ref, source, route)
            if ref is None:
                continue
            rows: list[dict] = []
            dr.seat_route_lines(route, trips[route], vcap[route], polls[route], ref, day, offday, source,
                                rows_out=rows)
            for r in rows:
                veh = trip_veh[route][r["trip"]]
                t = r["pass_time"]
                out.append({
                    "date": day, "weekday": weekday, "day_type": day_type, "special_note": special,
                    "source": source.replace("bus_", ""), "route": route, "period": r["period"],
                    "trip_id": f"{day}_{route}_{r['trip']:03d}", "vehicle": veh,
                    "capacity_est": vcap[route].get(veh, ""),
                    "mid_start": int(r["mid_start"]), "trip_first_seq": r["trip_first_seq"],
                    "trip_first_time": r["trip_first_time"].strftime("%H:%M:%S"),
                    "stop_seq": r["stop_seq"], "stop_name": r["stop_name"],
                    "stop_order": r["stop_order"], "n_stops": r["n_stops"],
                    "pass_time": t.strftime("%H:%M:%S"), "pass_min": t.hour * 60 + t.minute,
                    "arr_seats": r["arr_seats"], "dep_seats": "" if r["dep_seats"] is None else r["dep_seats"],
                    "arr": r["arr"], "dep": r["dep"],
                    "full_dep": {"full": 1, "ok": 0}.get(r["dep"], ""),
                })
    out.sort(key=lambda x: (x["route"], x["period"], x["stop_seq"], x["pass_time"], x["trip_id"]))
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def raw_days(paths: Paths) -> list[str]:
    days = set()
    for src in BUS_SOURCES:
        d = paths.raw / src
        if d.is_dir():
            days |= {p.name.split(".")[0] for p in d.iterdir() if p.name.startswith("20")}
    return sorted(days)


def raw_mtime(paths: Paths, day: str) -> float:
    files = dr.day_files(paths.raw, day)
    return max((files[s].stat().st_mtime for s in BUS_SOURCES if s in files), default=0)


def combine(tables: Path) -> int:
    n = 0
    parts = sorted(p for p in tables.glob("passes_20*.csv"))
    tmp = tables / "passes_all.tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=COLUMNS)
        w.writeheader()
        for p in parts:
            with open(p, encoding="utf-8-sig", newline="") as fi:
                for row in csv.DictReader(fi):
                    w.writerow(row)
                    n += 1
    tmp.replace(tables / "passes_all.csv")
    return n


def main() -> None:
    safe_console()
    ap = argparse.ArgumentParser(description="'운행 1회 × 정류장' 표 만들기")
    ap.add_argument("--date", help="이 날만 다시 만들기 (YYYY-MM-DD)")
    ap.add_argument("--from", dest="d0", help="시작 날짜")
    ap.add_argument("--to", dest="d1", help="끝 날짜(포함)")
    ap.add_argument("--rebuild", action="store_true", help="원본이 있는 모든 날을 다시 만들기")
    args = ap.parse_args()
    paths = Paths(BASE_DIR)
    tables = paths.data / "tables"
    today = now_kst().strftime("%Y-%m-%d")
    all_days = raw_days(paths)
    if args.date:
        todo = [args.date]
    elif args.d0 or args.d1:
        todo = [d for d in all_days if (not args.d0 or d >= args.d0) and (not args.d1 or d <= args.d1)]
    elif args.rebuild:
        todo = all_days
    else:  # 오늘은 수집 중이라 빼고, 표가 없거나 원본이 표보다 새로운 날
        todo = [d for d in all_days if d < today and (
            not (tables / f"passes_{d}.csv").exists()
            or (tables / f"passes_{d}.csv").stat().st_mtime < raw_mtime(paths, d))]
    if not todo:
        print("새로 만들 날짜가 없습니다.")
    for d in todo:
        rows = day_rows(BASE_DIR, d)
        write_csv(tables / f"passes_{d}.csv", rows)
        kinds = sorted({r["day_type"] for r in rows})
        print(f"{d}: {len(rows):,}줄 ({', '.join(kinds) or '자료 없음'}) → data/tables/passes_{d}.csv")
    n = combine(tables) if tables.exists() else 0
    print(f"전체 표: {n:,}줄 → data/tables/passes_all.csv (표 버전 {TABLE_VERSION})")


if __name__ == "__main__":
    main()
