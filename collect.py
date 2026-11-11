"""2단계: 파일럿 수집기. 켜 두면 config.yaml의 시간표대로 API를 호출해 원본을 저장한다.

실행:  python collect.py            (멈출 때는 Ctrl+C)
       python collect.py --once     (지금 시각 기준으로 한 바퀴만 돌고 종료. 설정 확인용)

저장 위치
  data/raw/<작업>/<YYYY-MM-DD>.jsonl   한 줄 = 호출 1회. 지난 날짜 파일은 자동으로 .jsonl.gz로 압축
  data/logs/collect.log               실행 기록 (자정마다 날짜별로 나뉨)
  data/logs/gaps.csv                  수집기가 꺼져 있었던 시간 (PC 절전·재부팅 등)
  data/state/quota_YYYY-MM-DD.json     API별 오늘 호출 수

먼저 check_apis.py를 한 번 실행해 노선 ID를 찾아 두어야 한다.
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import endpoints as ep
from common import (safe_console, BASE_DIR, DailyJsonlWriter, Http, Paths, Quota, Resp, api_message, as_list, epoch_slot, find_key,
                    is_offday, latlon_to_grid, load_config, load_env, now_kst, read_json, safe_params, safe_url,
                    setup_logging, tago_items, window_interval, write_json, ymd)

GAP_THRESHOLD_SEC = 180          # 이 이상 멈춰 있었으면 공백으로 기록
HEARTBEAT_EVERY_SEC = 30
SUMMARY_EVERY_SEC = 600


@dataclass
class Job:
    id: str            # 예) bus_gbis:7770
    schedule: str      # config.yaml schedules의 이름
    group: str         # 호출 한도 묶음
    fn: Callable[[datetime], None]


class Collector:
    def __init__(self, base_dir: Path = BASE_DIR, http: Optional[Http] = None, clock: Callable[[], datetime] = now_kst,
                 env: Optional[dict] = None, cfg: Optional[dict] = None):
        self.base_dir = base_dir
        self.clock = clock
        self.env = env if env is not None else load_env(base_dir)
        self.cfg = cfg if cfg is not None else load_config(base_dir)
        self.http = http or Http()
        self.paths = Paths(base_dir)
        q = self.cfg.get("quota", {})
        self.quota = Quota(self.paths.state, q.get("limits", {}), q.get("safety_ratio", 0.95), clock)
        self.writer = DailyJsonlWriter(self.paths.raw)
        self.seoul_base = self.cfg.get("seoul_bus_base", "http://ws.bus.go.kr/api/rest")
        self.holidays: set = set()
        self.last_slot: dict = {}
        self.stats: dict = {}
        self.fail_counts: dict = {}
        self.ex_seen: set = set()
        self.day = ""
        self.last_tick: Optional[datetime] = None
        self.last_heartbeat = 0.0
        self.last_summary = 0.0
        self.resolved = read_json(self.paths.state / "resolved_routes.json", {}) or {}
        self.jobs = self.build_jobs()

    # ------------------------------------------------------------------ 준비
    def route_ids(self, r: dict) -> dict:
        name = r["name"]
        res = self.resolved.get(name, {})
        out = {}
        g = r.get("gbis") or {}
        out["gbis"] = str(g.get("route_id") or res.get("gbis_route_id") or "")
        t = r.get("tago") or {}
        out["tago"] = str(t.get("route_id") or res.get("tago_route_id") or "")
        out["tago_city"] = res.get("tago_city_code") or ((t.get("city_codes") or [None])[0])
        s = r.get("seoul") or {}
        out["seoul"] = str(s.get("route_id") or res.get("seoul_route_id") or "")
        return out

    def build_jobs(self) -> list[Job]:
        jobs: list[Job] = []
        key = self.env.get("DATA_GO_KR_KEY", "")
        missing = []
        for r in self.cfg.get("routes", []):
            ids = self.route_ids(r)
            name = r["name"]
            if "gbis" in r:
                if ids["gbis"]:
                    jobs.append(Job(f"bus_gbis:{name}", "bus_gbis", "gbis_buslocation",
                                    lambda now, r=r, rid=ids["gbis"]: self.poll_bus("bus_gbis", r["name"], rid, now)))
                else:
                    missing.append(f"{name}(GBIS)")
            if "tago" in r:
                if ids["tago"] and ids["tago_city"]:
                    jobs.append(Job(f"bus_tago:{name}", "bus_tago", "tago_buslc",
                                    lambda now, r=r, rid=ids["tago"], c=ids["tago_city"]: self.poll_bus("bus_tago", r["name"], rid, now, c)))
                else:
                    missing.append(f"{name}(TAGO)")
            if "seoul" in r:
                if ids["seoul"]:
                    jobs.append(Job(f"bus_seoul:{name}", "bus_seoul", "seoul_buspos",
                                    lambda now, r=r, rid=ids["seoul"]: self.poll_bus("bus_seoul", r["name"], rid, now)))
                else:
                    missing.append(f"{name}(서울)")
        if missing:
            logging.warning("노선 ID가 없어 수집하지 않는 노선: %s  → check_apis.py를 먼저 실행하세요.", ", ".join(missing))
        if not key:
            logging.warning(".env에 DATA_GO_KR_KEY가 없습니다. 버스·기상 수집이 모두 실패합니다.")

        rcfg = self.cfg.get("road_ex", {})
        if self.env.get("EX_KEY") and (rcfg.get("realtime_url") or "").strip():
            jobs.append(Job("road_ex", "road_ex", "ex_realtime", self.poll_road_ex))
        else:
            logging.info("도로공사 수집 건너뜀 (EX_KEY 또는 road_ex.realtime_url 비어 있음)")

        seen_grid = set()
        for p in self.cfg.get("weather_points", []):
            nx, ny = latlon_to_grid(p["lat"], p["lon"])
            if (nx, ny) in seen_grid:
                continue
            seen_grid.add((nx, ny))
            jobs.append(Job(f"weather:{p['name']}", "weather", "kma_vilage",
                            lambda now, p=p, nx=nx, ny=ny: self.poll_weather(p["name"], nx, ny, now)))

        st = self.cfg.get("seoul_traffic", {})
        if self.env.get("SEOUL_KEY"):
            for link in st.get("link_ids", []) or []:
                jobs.append(Job(f"seoul_link:{link}", "seoul_link", "seoul_traffic",
                                lambda now, link=str(link): self.poll_seoul_link(link, now)))
        logging.info("수집 작업 %d개: %s", len(jobs), ", ".join(j.id for j in jobs))
        return jobs

    # ------------------------------------------------------------------ 공통 기록
    def _save(self, job: str, now: datetime, resp: Resp, extra: dict, items: list) -> bool:
        code, msg = api_message(resp)
        ok = (resp.status == 200 and resp.json is not None)
        rec = {"ts": now.isoformat(timespec="seconds"), "job": job, "url": safe_url(resp.url),
               "params": safe_params(resp.params), "http": resp.status, "ms": resp.ms, "code": code,
               "n": len(items)}
        rec.update(extra)
        if not ok:
            rec["msg"] = msg
            rec["text"] = (resp.text or resp.error)[:2000]
        rec["items"] = items
        self.writer.write(job.split(":")[0], rec, now)
        st = self.stats.setdefault(job, {"calls": 0, "ok": 0, "items": 0})
        st["calls"] += 1
        st["ok"] += int(ok)
        st["items"] += len(items)
        if not ok:
            c = self.fail_counts.get(job, 0) + 1
            self.fail_counts[job] = c
            if c <= 3 or c % 30 == 0:
                logging.warning("[실패] %s http=%s code=%s msg=%s (누적 %d회)", job, resp.status, code, msg[:120], c)
        return ok

    # ------------------------------------------------------------------ 버스
    def poll_bus(self, source: str, name: str, route_id: str, now: datetime, city: Optional[int] = None) -> None:
        key = self.env.get("DATA_GO_KR_KEY", "")
        if source == "bus_gbis":
            group, up = "gbis_buslocation", ep.gbis_bus_location(key, route_id)
        elif source == "bus_tago":
            group, up = "tago_buslc", ep.tago_bus_location(key, city, route_id)
        else:
            group, up = "seoul_buspos", ep.seoul_bus_pos(key, route_id, self.seoul_base)
        resp = self.http.get(*up)
        self.quota.add(group)
        j = resp.json
        if source == "bus_gbis":
            items = as_list(find_key(j, "busLocationList"))
        elif source == "bus_tago":
            items = tago_items(j)
        else:
            items = as_list(find_key(j, "itemList"))
        self._save(f"{source}:{name}", now, resp, {"route": name, "route_id": route_id}, items)

    # ------------------------------------------------------------------ 도로공사
    def _ex_first_day(self, today: str) -> str:
        p = self.paths.state / "ex_first_day.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or today
        p.write_text(today, encoding="utf-8")
        return today

    def poll_road_ex(self, now: datetime) -> None:
        rcfg = self.cfg.get("road_ex", {})
        url = rcfg["realtime_url"].strip()
        resp = self.http.get(*ep.ex_realtime(self.env["EX_KEY"], url, rcfg.get("extra_params")))
        self.quota.add("ex_realtime")
        rows = as_list(find_key(resp.json, "list"))
        today = ymd(now)
        first = datetime.strptime(self._ex_first_day(today), "%Y-%m-%d").date()
        full = (now.date() - first).days < int(rcfg.get("full_dump_days", 2))
        keep_routes = {str(x) for x in rcfg.get("keep_route_nos") or []}
        keep_cz = {str(x) for x in rcfg.get("keep_conzone_ids") or []}
        extra = {"n_total": len(rows), "mode": "full" if full else "filtered"}
        if full:
            items = rows
        else:
            if keep_cz:
                filt = [r for r in rows if str(r.get("conzoneId")) in keep_cz]
            elif keep_routes:
                filt = [r for r in rows if str(r.get("routeNo")) in keep_routes]
            else:
                filt = rows
            items = []
            for r in filt:
                k = (r.get("conzoneId"), r.get("updownTypeCode"), r.get("stdDate"), r.get("stdHour"))
                if k in self.ex_seen:
                    continue
                self.ex_seen.add(k)
                items.append(r)
            extra.update({"n_filtered": len(filt), "n_new": len(items)})
        self._save("road_ex", now, resp, extra, items)

    # ------------------------------------------------------------------ 기상
    def poll_weather(self, name: str, nx: int, ny: int, now: datetime) -> None:
        key = self.env.get("DATA_GO_KR_KEY", "")
        base = now.replace(minute=0, second=0, microsecond=0)
        resp = self.http.get(*ep.kma_ultra_ncst(key, base.strftime("%Y%m%d"), base.strftime("%H%M"), nx, ny))
        self.quota.add("kma_vilage")
        items = tago_items(resp.json)
        if not items:  # 아직 안 올라왔으면 한 시간 전 자료라도 받는다
            base -= timedelta(hours=1)
            resp = self.http.get(*ep.kma_ultra_ncst(key, base.strftime("%Y%m%d"), base.strftime("%H%M"), nx, ny))
            self.quota.add("kma_vilage")
            items = tago_items(resp.json)
        self._save(f"weather:{name}", now, resp, {"point": name, "nx": nx, "ny": ny,
                                                  "base": base.strftime("%Y%m%d%H%M")}, items)

    # ------------------------------------------------------------------ 서울 도로
    def poll_seoul_link(self, link: str, now: datetime) -> None:
        st = self.cfg.get("seoul_traffic", {})
        resp = self.http.get(*ep.seoul_open(self.env["SEOUL_KEY"], st.get("service", "TrafficInfo"), 1, 5, link))
        self.quota.add("seoul_traffic")
        items = as_list(find_key(resp.json, "row"))
        self._save(f"seoul_link:{link}", now, resp, {"link_id": link}, items)

    # ------------------------------------------------------------------ 공휴일
    def refresh_holidays(self, now: datetime) -> None:
        key = self.env.get("DATA_GO_KR_KEY", "")
        years = {now.year} | ({now.year + 1} if now.month == 12 else set())
        days: set = set()
        for y in sorted(years):
            p = self.paths.state / f"holidays_{y}.json"
            cached = read_json(p, None)
            fresh = p.exists() and (time.time() - p.stat().st_mtime) < 7 * 86400
            if cached is not None and fresh:
                days |= set(cached)
                continue
            if not key or not self.quota.allow("kasi_spcde"):
                days |= set(cached or [])
                continue
            got: set = set()
            ok_all = True
            for m in range(1, 13):
                resp = self.http.get(*ep.kasi_holidays(key, y, m))
                self.quota.add("kasi_spcde")
                if resp.status != 200 or resp.json is None:
                    ok_all = False
                    break
                for it in tago_items(resp.json):
                    if str(it.get("isHoliday", "Y")) == "Y":
                        got.add(str(it.get("locdate")))
            if ok_all:
                write_json(p, sorted(got))
                days |= got
                logging.info("%d년 공휴일 %d일 갱신", y, len(got))
            else:
                logging.warning("%d년 공휴일을 받지 못해 이전 목록을 씁니다 (없으면 토·일만 주말로 처리).", y)
                days |= set(cached or [])
        self.holidays = days

    # ------------------------------------------------------------------ 공백·하루 넘김
    def record_gap(self, start: datetime, end: datetime, reason: str) -> None:
        p = self.paths.logs / "gaps.csv"
        new = not p.exists()
        mins = round((end - start).total_seconds() / 60, 1)
        with open(p, "a", encoding="utf-8-sig" if new else "utf-8") as f:
            if new:
                f.write("start,end,minutes,reason\n")
            f.write(f"{start.isoformat(timespec='seconds')},{end.isoformat(timespec='seconds')},{mins},{reason}\n")
        logging.warning("수집 공백 기록: %s ~ %s (%.1f분, %s)", start, end, mins, reason)

    def check_startup_gap(self, now: datetime) -> None:
        hb = read_json(self.paths.state / "heartbeat.json", None)
        if hb and hb.get("ts"):
            last = datetime.fromisoformat(hb["ts"])
            if (now - last).total_seconds() > GAP_THRESHOLD_SEC:
                self.record_gap(last, now, "수집기 꺼짐/재시작")

    def heartbeat(self, now: datetime, force: bool = False) -> None:
        t = now.timestamp()
        if force or t - self.last_heartbeat >= HEARTBEAT_EVERY_SEC:
            write_json(self.paths.state / "heartbeat.json", {"ts": now.isoformat(timespec="seconds")})
            self.last_heartbeat = t

    def on_new_day(self, now: datetime) -> None:
        today = ymd(now)
        if self.day:
            logging.info("하루 마감 %s 호출 수: %s", self.day, self.stats)
        self.day = today
        self.stats = {}
        self.ex_seen = set()
        n = self.writer.compress_old(today)
        if n:
            logging.info("지난 날짜 원본 %d개 압축", n)
        self.refresh_holidays(now)

    # ------------------------------------------------------------------ 한 바퀴
    def tick(self) -> int:
        now = self.clock()
        if self.last_tick is not None and (now - self.last_tick).total_seconds() > GAP_THRESHOLD_SEC:
            self.record_gap(self.last_tick, now, "실행 중 멈춤(절전 등)")
        self.last_tick = now
        if ymd(now) != self.day:
            self.on_new_day(now)
        offday = is_offday(now, self.holidays)
        sched = self.cfg.get("schedules", {})
        ran = 0
        for job in self.jobs:
            iv = window_interval(sched.get(job.schedule, []), now, offday)
            if iv is None:
                continue
            every, offset = iv
            slot = (every, epoch_slot(now, every, offset))
            if self.last_slot.get(job.id) == slot:
                continue
            self.last_slot[job.id] = slot
            if not self.quota.allow(job.group):
                continue
            try:
                job.fn(now)
                ran += 1
            except Exception as e:  # 한 작업이 실패해도 다른 작업은 계속한다
                logging.exception("[오류] %s: %s", job.id, e)
        self.heartbeat(now)
        t = now.timestamp()
        if t - self.last_summary >= SUMMARY_EVERY_SEC:
            if self.stats:
                summ = ", ".join(f"{k} {v['ok']}/{v['calls']}" for k, v in sorted(self.stats.items()))
                logging.info("[요약] 오늘 성공/호출: %s", summ)
            self.last_summary = t
        return ran

    def run_forever(self) -> None:
        now = self.clock()
        self.check_startup_gap(now)
        logging.info("수집 시작 (멈추려면 Ctrl+C)")
        try:
            while True:
                self.tick()
                time.sleep(max(0.2, 1.0 - (time.time() % 1.0)))
        except KeyboardInterrupt:
            logging.info("사용자가 수집을 멈췄습니다.")
        finally:
            self.heartbeat(self.clock(), force=True)


def main() -> None:
    safe_console()
    ap = argparse.ArgumentParser(description="광역버스 소요시간 예측 파일럿 수집기")
    ap.add_argument("--once", action="store_true", help="지금 시각 기준으로 한 바퀴만 돌고 종료")
    args = ap.parse_args()
    paths = Paths(BASE_DIR)
    setup_logging(paths.logs, "collect")
    c = Collector()
    if args.once:
        c.check_startup_gap(c.clock())
        n = c.tick()
        logging.info("--once: 이번 바퀴에 실행한 작업 %d개 (시간표에 해당하지 않으면 0개일 수 있음)", n)
        c.heartbeat(c.clock(), force=True)
    else:
        c.run_forever()


if __name__ == "__main__":
    main()
