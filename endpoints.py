"""API 주소와 요청 파라미터를 한곳에 모은 파일.

각 함수는 (url, params) 튜플을 돌려준다. 실제 호출은 common.Http가 한다.
공공데이터포털 키는 'Decoding 키'를 그대로 넣는다(requests가 알아서 인코딩한다).

주소 출처
- 경기(GBIS v2): gbis.go.kr 공유서비스 매뉴얼
- 서울 버스: 공공데이터포털 서울특별시_버스위치정보조회 서비스 등 (ws.bus.go.kr)
- TAGO: 공공데이터포털 국토교통부_(TAGO)_버스노선정보 등
- 기상청 단기예보, 천문연 특일정보, 기상청 ASOS: 공공데이터포털
- 도로공사: data.ex.co.kr (0405 주소는 config.yaml에 직접 적는다)
- 서울 도로소통: 서울 열린데이터광장 (openapi.seoul.go.kr:8088)
"""
from __future__ import annotations

DG = "https://apis.data.go.kr"
GBIS = f"{DG}/6410000"
TAGO = f"{DG}/1613000"


# ---------------- 경기도 GBIS (v2) ----------------
def gbis_route_search(key: str, keyword: str):
    return f"{GBIS}/busrouteservice/v2/getBusRouteListv2", {"serviceKey": key, "keyword": keyword, "format": "json"}


def gbis_route_info(key: str, route_id: str):
    return f"{GBIS}/busrouteservice/v2/getBusRouteInfoItemv2", {"serviceKey": key, "routeId": route_id, "format": "json"}


def gbis_route_stations(key: str, route_id: str):
    return f"{GBIS}/busrouteservice/v2/getBusRouteStationListv2", {"serviceKey": key, "routeId": route_id, "format": "json"}


def gbis_bus_location(key: str, route_id: str):
    return f"{GBIS}/buslocationservice/v2/getBusLocationListv2", {"serviceKey": key, "routeId": route_id, "format": "json"}


def gbis_arrival_list(key: str, station_id: str):
    return f"{GBIS}/busarrivalservice/v2/getBusArrivalListv2", {"serviceKey": key, "stationId": station_id, "format": "json"}


def gbis_station_search(key: str, keyword: str):
    return f"{GBIS}/busstationservice/v2/getBusStationListv2", {"serviceKey": key, "keyword": keyword, "format": "json"}


# ---------------- TAGO (경기 시군만 제공, 서울 없음) ----------------
def tago_route_no(key: str, city_code: int, route_no: str):
    return f"{TAGO}/BusRouteInfoInqireService/getRouteNoList", {
        "serviceKey": key, "cityCode": city_code, "routeNo": route_no, "_type": "json", "numOfRows": 50, "pageNo": 1}


def tago_route_stations(key: str, city_code: int, route_id: str):
    return f"{TAGO}/BusRouteInfoInqireService/getRouteAcctoThrghSttnList", {
        "serviceKey": key, "cityCode": city_code, "routeId": route_id, "_type": "json", "numOfRows": 300, "pageNo": 1}


def tago_bus_location(key: str, city_code: int, route_id: str):
    return f"{TAGO}/BusLcInfoInqireService/getRouteAcctoBusLcList", {
        "serviceKey": key, "cityCode": city_code, "routeId": route_id, "_type": "json", "numOfRows": 100, "pageNo": 1}


def tago_arrival(key: str, city_code: int, node_id: str):
    return f"{TAGO}/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList", {
        "serviceKey": key, "cityCode": city_code, "nodeId": node_id, "_type": "json", "numOfRows": 100, "pageNo": 1}


def tago_city_codes(key: str):
    return f"{TAGO}/BusRouteInfoInqireService/getCtyCodeList", {"serviceKey": key, "_type": "json"}


# ---------------- 서울 버스 (ws.bus.go.kr) ----------------
def seoul_route_list(key: str, keyword: str, base: str):
    return f"{base}/busRouteInfo/getBusRouteList", {"serviceKey": key, "strSrch": keyword, "resultType": "json"}


def seoul_route_stations(key: str, route_id: str, base: str):
    return f"{base}/busRouteInfo/getStaionByRoute", {"serviceKey": key, "busRouteId": route_id, "resultType": "json"}


def seoul_bus_pos(key: str, route_id: str, base: str):
    return f"{base}/buspos/getBusPosByRtid", {"serviceKey": key, "busRouteId": route_id, "resultType": "json"}


def seoul_arrival_route(key: str, route_id: str, base: str):
    return f"{base}/arrive/getArrInfoByRouteAll", {"serviceKey": key, "busRouteId": route_id, "resultType": "json"}


def seoul_station_by_uid(key: str, ars_id: str, base: str):
    return f"{base}/stationinfo/getStationByUid", {"serviceKey": key, "arsId": ars_id, "resultType": "json"}


# ---------------- 기상청·천문연 ----------------
def kma_ultra_ncst(key: str, base_date: str, base_time: str, nx: int, ny: int):
    """초단기실황: 매시 정시 관측값. 보통 매시 40분 이후에 그 시각 자료가 올라온다."""
    return f"{DG}/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst", {
        "serviceKey": key, "pageNo": 1, "numOfRows": 100, "dataType": "JSON",
        "base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny}


def kasi_holidays(key: str, year: int, month: int):
    return f"{DG}/B090041/openapi/service/SpcdeInfoService/getRestDeInfo", {
        "serviceKey": key, "solYear": year, "solMonth": f"{month:02d}", "_type": "json", "numOfRows": 50}


def kma_asos_hourly(key: str, stn_id: int, day_yyyymmdd: str):
    return f"{DG}/1360000/AsosHourlyInfoService/getWthrDataList", {
        "serviceKey": key, "pageNo": 1, "numOfRows": 24, "dataType": "JSON", "dataCd": "ASOS", "dateCd": "HR",
        "startDt": day_yyyymmdd, "startHh": "01", "endDt": day_yyyymmdd, "endHh": "23", "stnIds": stn_id}


# ---------------- 한국도로공사 ----------------
EX_CONGEST_URL = "https://data.ex.co.kr/openapi/odtraffic/trafficAmountByCongest"


def ex_realtime(ex_key: str, url: str, extra: dict | None = None):
    params = {"key": ex_key, "type": "json"}
    params.update(extra or {})
    return url, params


def ex_congest(ex_key: str):
    return EX_CONGEST_URL, {"key": ex_key, "type": "json"}


# ---------------- 서울 열린데이터광장 ----------------
def seoul_open(seoul_key: str, service: str, start: int, end: int, *args: str):
    """형식: http://openapi.seoul.go.kr:8088/{인증키}/json/{서비스명}/{시작}/{끝}/{추가인자...}"""
    tail = "/".join(str(a) for a in args if a not in (None, ""))
    url = f"http://openapi.seoul.go.kr:8088/{seoul_key}/json/{service}/{start}/{end}/"
    if tail:
        url += tail + "/"
    return url, {}
