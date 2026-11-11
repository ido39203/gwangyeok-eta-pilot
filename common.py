"""공통 도구: 설정·키 읽기, HTTP 호출, 응답 해석, 날짜별 저장, 호출 한도 관리.

check_apis.py, collect.py, daily_report.py가 모두 이 파일을 가져다 쓴다.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import requests
import yaml

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = Path(__file__).resolve().parent

# 응답 원본에 남기면 안 되는 파라미터(인증키)
SECRET_PARAMS = {"serviceKey", "servicekey", "key", "apikey", "apiKey", "KEY"}


# ---------------------------------------------------------------------------
# 시간
# ---------------------------------------------------------------------------
def safe_console() -> None:
    """윈도우 명령 프롬프트(cp949)에서 출력할 수 없는 글자가 있어도 멈추지 않게 한다."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def now_kst() -> datetime:
    return datetime.now(KST)


def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 설정과 인증키
# ---------------------------------------------------------------------------
def load_env(base_dir: Path = BASE_DIR) -> dict:
    """.env 파일에서 인증키를 읽는다. 값이 비어 있으면 빈 문자열."""
    try:
        from dotenv import load_dotenv

        load_dotenv(base_dir / ".env")
    except ImportError:  # python-dotenv가 없으면 환경변수만 사용
        pass
    return {
        "DATA_GO_KR_KEY": os.getenv("DATA_GO_KR_KEY", "").strip(),
        "EX_KEY": os.getenv("EX_KEY", "").strip(),
        "SEOUL_KEY": os.getenv("SEOUL_KEY", "").strip(),
    }


def load_config(base_dir: Path = BASE_DIR) -> dict:
    with open(base_dir / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def mask(s: str) -> str:
    if not s:
        return "(비어 있음)"
    return s[:4] + "..." + s[-4:] if len(s) > 10 else "****"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@dataclass
class Resp:
    url: str
    params: dict
    status: Optional[int] = None
    text: str = ""
    json: Any = None
    ms: int = 0
    error: str = ""


class Http:
    """requests 래퍼. 연결 오류는 한 번만 다시 시도한다."""

    def __init__(self, timeout: float = 15.0, retries: int = 1, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "gwangyeok-eta-pilot/0.1"})

    def get(self, url: str, params: Optional[dict] = None) -> Resp:
        params = params or {}
        last_err = ""
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                ms = int((time.monotonic() - t0) * 1000)
                text = r.text
                return Resp(url, params, r.status_code, text, parse_json(text), ms)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < self.retries:
                    time.sleep(3)
        return Resp(url, params, None, "", None, 0, last_err[:300])


def parse_json(text: str) -> Any:
    if not text:
        return None
    t = text.lstrip()
    if not (t.startswith("{") or t.startswith("[")):
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def safe_params(params: dict) -> dict:
    return {k: v for k, v in (params or {}).items() if k not in SECRET_PARAMS}


def safe_url(url: str) -> str:
    """서울 열린데이터광장처럼 키가 주소 안에 들어가는 경우를 가린다."""
    return re.sub(r"(openapi\.seoul\.go\.kr:8088/)[^/]+/", r"\1<KEY>/", url)


# ---------------------------------------------------------------------------
# 응답 해석
# ---------------------------------------------------------------------------
def find_key(obj: Any, key: str) -> Any:
    """중첩된 dict/list에서 key를 가진 첫 값을 찾는다(깊이 우선)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


def as_list(x: Any) -> list:
    """공공 API는 0건이면 ""나 None, 1건이면 dict, 여러 건이면 list를 준다. 항상 list로 맞춘다."""
    if x is None or x == "" or x == {}:
        return []
    if isinstance(x, dict):
        return [x]
    if isinstance(x, list):
        return x
    return []


def tago_items(j: Any) -> list:
    """TAGO·기상청·천문연 형식: response.body.items.item"""
    items = find_key(j, "items")
    if isinstance(items, dict):
        return as_list(items.get("item"))
    return as_list(items)


_CODE_KEYS = ["resultCode", "headerCd", "CODE", "code", "returnReasonCode"]
_MSG_KEYS = ["resultMsg", "resultMessage", "headerMsg", "MESSAGE", "message", "returnAuthMsg", "errMsg"]


def api_message(resp: Resp) -> tuple[str, str]:
    """응답에서 (결과코드, 메시지)를 최대한 뽑아낸다. JSON이 아니면 XML 텍스트에서 찾는다."""
    if resp.error:
        return "NETWORK", resp.error
    j = resp.json
    code, msg = "", ""
    if j is not None:
        for k in _CODE_KEYS:
            v = find_key(j, k)
            if v not in (None, ""):
                code = str(v)
                break
        for k in _MSG_KEYS:
            v = find_key(j, k)
            if v not in (None, ""):
                msg = str(v)
                break
        return code, msg
    text = resp.text or ""
    for k in ["returnReasonCode", "resultCode", "headerCd"]:
        m = re.search(rf"<{k}>(.*?)</{k}>", text, re.S)
        if m:
            code = m.group(1).strip()
            break
    for k in ["returnAuthMsg", "errMsg", "resultMsg", "headerMsg"]:
        m = re.search(rf"<{k}>(.*?)</{k}>", text, re.S)
        if m:
            msg = m.group(1).strip()
            break
    if not msg:
        msg = re.sub(r"\s+", " ", text)[:200]
    return code, msg


# ---------------------------------------------------------------------------
# 기상청 격자 변환 (위경도 -> nx, ny). 기상청 단기예보 활용가이드의 LCC 공식.
# ---------------------------------------------------------------------------
def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    import math

    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136
    DEGRAD = math.pi / 180.0
    re_ = RE / GRID
    slat1, slat2 = SLAT1 * DEGRAD, SLAT2 * DEGRAD
    olon, olat = OLON * DEGRAD, OLAT * DEGRAD
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re_ * sf / math.pow(ro, sn)
    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5)
    ra = re_ * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    nx = int(math.floor(ra * math.sin(theta) + XO + 0.5))
    ny = int(math.floor(ro - ra * math.cos(theta) + YO + 0.5))
    return nx, ny


# ---------------------------------------------------------------------------
# 저장: data/raw/<job>/<YYYY-MM-DD>.jsonl  (지난 날짜 파일은 .jsonl.gz로 압축)
# ---------------------------------------------------------------------------
class DailyJsonlWriter:
    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._last_compress_day = ""

    def path_for(self, job: str, day: str) -> Path:
        d = self.raw_dir / job
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{day}.jsonl"

    def write(self, job: str, record: dict, when: datetime) -> None:
        p = self.path_for(job, ymd(when))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def compress_old(self, today: str) -> int:
        """오늘이 아닌 .jsonl 파일을 gzip으로 압축한다. 하루 한 번만 실행."""
        if self._last_compress_day == today:
            return 0
        n = 0
        for p in self.raw_dir.glob("*/*.jsonl"):
            if p.stem == today:
                continue
            gz = p.with_suffix(".jsonl.gz")
            with open(p, "rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            n += 1
        self._last_compress_day = today
        return n


def read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # 수집기가 쓰는 도중 꺼지면 마지막 줄이 잘릴 수 있다. 그 줄만 건너뛴다.
                continue


# ---------------------------------------------------------------------------
# 호출 한도: state/quota_YYYY-MM-DD.json 에 API 묶음별 오늘 호출 수를 센다.
# check_apis.py와 collect.py가 같은 파일을 쓰므로 점검 호출도 한도에 포함된다.
# ---------------------------------------------------------------------------
class Quota:
    def __init__(self, state_dir: Path, limits: dict, safety_ratio: float = 0.95, clock: Callable[[], datetime] = now_kst):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.limits = limits or {}
        self.safety_ratio = safety_ratio
        self.clock = clock
        self._warned: set = set()

    def _path(self) -> Path:
        return self.state_dir / f"quota_{ymd(self.clock())}.json"

    def _load(self) -> dict:
        p = self._path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self, d: dict) -> None:
        p = self._path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def used(self, group: str) -> int:
        return int(self._load().get(group, 0))

    def cap(self, group: str) -> Optional[int]:
        lim = self.limits.get(group)
        if lim is None:
            return None
        return int(lim * self.safety_ratio)

    def allow(self, group: str) -> bool:
        cap = self.cap(group)
        if cap is None:
            return True
        ok = self.used(group) < cap
        key = (ymd(self.clock()), group)
        if not ok and key not in self._warned:
            logging.warning("[한도] %s 오늘 호출 %d회로 안전 한도 %d회에 도달. 오늘은 더 호출하지 않음.", group, self.used(group), cap)
            self._warned.add(key)
        return ok

    def add(self, group: str, n: int = 1) -> None:
        d = self._load()
        d[group] = int(d.get(group, 0)) + n
        self._save(d)

    def snapshot(self) -> dict:
        return self._load()


# ---------------------------------------------------------------------------
# 경로 묶음
# ---------------------------------------------------------------------------
@dataclass
class Paths:
    base: Path
    data: Path = field(init=False)
    raw: Path = field(init=False)
    state: Path = field(init=False)
    logs: Path = field(init=False)
    ref: Path = field(init=False)
    checks: Path = field(init=False)
    reports: Path = field(init=False)

    def __post_init__(self):
        self.data = self.base / "data"
        self.raw = self.data / "raw"
        self.state = self.data / "state"
        self.logs = self.data / "logs"
        self.ref = self.data / "ref"
        self.checks = self.data / "check_results"
        self.reports = self.data / "reports"
        for p in [self.raw, self.state, self.logs, self.ref, self.checks, self.reports]:
            p.mkdir(parents=True, exist_ok=True)


def setup_logging(logs_dir: Path, name: str) -> None:
    from logging.handlers import TimedRotatingFileHandler

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = TimedRotatingFileHandler(logs_dir / f"{name}.log", when="midnight", backupCount=60, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_offday(day: datetime, holidays: set) -> bool:
    """토·일·공휴일이면 True (시간대 유형 HOL과 같은 기준)."""
    return day.weekday() >= 5 or day.strftime("%Y%m%d") in holidays


def parse_hhmm(s: str) -> int:
    """'06:00' -> 360 (분). '24:00'도 허용."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def window_interval(windows: list, when: datetime, offday: bool) -> Optional[tuple[int, int]]:
    """지금 시각에 해당하는 (호출 간격 초, 오프셋 초)를 돌려준다. 해당 창이 없으면 None."""
    minute = when.hour * 60 + when.minute
    for w in windows or []:
        days = w.get("days", "all")
        if days == "weekday" and offday:
            continue
        if days == "weekend" and not offday:
            continue
        if parse_hhmm(w["start"]) <= minute < parse_hhmm(w["end"]):
            return int(w["every_sec"]), int(w.get("offset_sec", 0))
    return None


def epoch_slot(when: datetime, every_sec: int, offset_sec: int = 0) -> int:
    """같은 간격 안에서는 같은 번호가 나온다. 시계에 맞춰 호출 시점을 정렬하는 데 쓴다."""
    return int((when.timestamp() - offset_sec) // every_sec)
