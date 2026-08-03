# -*- coding: utf-8 -*-
"""
shipxy_locator.py  —  船位定位引擎（可命令行、可被 GUI import）

管线（三步，均已实测打通）:
  1) 船名  → MMSI      : searchv4.shipxy.com 搜索接口，取 ship[0].m（无反爬）
  2) MMSI → 经纬度      : Playwright 真实 Chromium 打开 shipxy，过网易易盾，
                          页面内用站点签名器 window.R0VOQ1NJR04 发 /ship/GetShipm，
                          取 data[0].lat/1e6、lon/1e6（WGS-84）
  3) 经纬度 → 结构化地址 : 逆地理编码链，输出 国家/省/市/县区/镇/道路/POI（国内外均详细）

命令行:
    python shipxy_locator.py 渝东809 413970005 --json
GUI 用法见 shipxy_gui.py。
"""

import sys
import os
import json
import time
import math
import argparse

from curl_cffi import requests as cffi

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _app_dir():
    """程序目录：打包(exe)后为 exe 所在目录，否则为脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _setup_playwright_browsers():
    """打包(exe)后定位 Playwright 浏览器内核：优先随包内置，其次系统已安装。"""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    cands = []
    base = getattr(sys, "_MEIPASS", None)          # PyInstaller 解压目录
    if base:
        cands.append(os.path.join(base, "ms-playwright"))
    cands.append(os.path.join(_app_dir(), "ms-playwright"))
    for env in ("LOCALAPPDATA", "USERPROFILE"):
        v = os.environ.get(env)
        if v:
            cands.append(os.path.join(v, "ms-playwright"))
            cands.append(os.path.join(v, "AppData", "Local", "ms-playwright"))
    for c in cands:
        if c and os.path.isdir(c):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = c
            return


_setup_playwright_browsers()

NAVI_STATUS = {                        # 与官网 base.js getNaviStatus 完全一致
    0: "在航(主机推动)", 1: "锚泊", 2: "失控", 3: "操作受限", 4: "吃水受限",
    5: "靠泊", 6: "搁浅", 7: "捕捞作业", 8: "靠船帆提供动力",
}


# ─────────────────────────────────────────────────────────────────────────────
# 第 1 步：船名 → MMSI
# ─────────────────────────────────────────────────────────────────────────────
def search_ship(keyword, timeout=20):
    r = cffi.get("https://searchv4.shipxy.com/index.ashx",
                 params={"f": "auto", "kw": keyword},
                 headers={"User-Agent": UA, "Referer": "https://www.shipxy.com/"},
                 impersonate="chrome", timeout=timeout)
    try:
        return r.json().get("ship", []) or []
    except Exception:
        return []


def resolve_mmsi(query):
    """输入 MMSI(7~9位数字) 或 船名 → (mmsi, 命中项or None)。"""
    q = str(query).strip()
    if q.isdigit() and 7 <= len(q) <= 9:
        return q, None
    ships = search_ship(q)
    if not ships:
        raise LookupError(f"未搜索到船舶：{query}")
    return str(ships[0]["m"]), ships[0]


# ─────────────────────────────────────────────────────────────────────────────
# 第 2 步：MMSI → 经纬度（Playwright 过易盾）
# ─────────────────────────────────────────────────────────────────────────────
_STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
"""

_QUERY_JS = """(mmsi) => new Promise((resolve) => {
    if (!window.jQuery || !window.R0VOQ1NJR04) { resolve({status:-2}); return; }
    window.jQuery.ajax({ url:'/ship/GetShipm', type:'POST', dataType:'json',
        data:{ shipIDs: mmsi, mmsi: mmsi },
        success:(d)=>resolve(d), error:(x)=>resolve({status:-1, __http:x.status}) });
})"""


class ShipxyLocator:
    """浏览器会话；打开一次可批量查询。用作上下文管理器。"""

    def __init__(self, headless=True, timeout=60000):
        self.headless = headless
        self.timeout = timeout
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"])
        self.ctx = self.browser.new_context(          # 内存上下文，不落盘(不生成 profile 目录)
            user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")
        self.ctx.add_init_script(_STEALTH)
        self.page = self.ctx.new_page()
        self._open()
        return self

    def _open(self):
        self.page.goto("https://www.shipxy.com/", wait_until="networkidle", timeout=self.timeout)
        try:
            self.page.wait_for_function("() => window.jQuery && window.R0VOQ1NJR04", timeout=self.timeout)
        except Exception:
            pass
        self.page.wait_for_timeout(3000)

    def get_ship(self, mmsi, retries=3):
        mmsi = str(mmsi)
        last = None
        for _ in range(retries):
            d = self.page.evaluate(_QUERY_JS, mmsi)
            last = d
            st = d.get("status")
            if st == 0 and d.get("data"):
                return self._parse(d["data"][0])
            if st == 0 and not d.get("data"):
                raise LookupError(f"MMSI {mmsi} 无船位数据（可能长期未上报）")
            if st in (2, -2):
                self._open()
                continue
            if st in (110, 112):
                raise PermissionError(f"触发人工验证(status={st})：请用有头模式手动通过一次")
            self.page.wait_for_timeout(1500)
        raise RuntimeError(f"获取失败 MMSI={mmsi}，最后响应：{last}")

    @staticmethod
    def _parse(d):
        hdg = d.get("hdg", 51100) / 100.0
        lat = round(d["lat"] / 1e6, 6)
        lon = round(d["lon"] / 1e6, 6)
        return {
            "mmsi": d.get("mmsi"), "name": d.get("name"),
            "cnname": (d.get("cnname") or "").replace("%", ""),
            "imo": d.get("imo"), "callsign": d.get("callsign"),
            "lat": lat, "lon": lon,
            "lat_dms": _dms(lat, "N", "S"), "lon_dms": _dms(lon, "E", "W"),  # 度分格式，与官网一致
            "sog_knots": round(d.get("sog", 0) / 514.0, 2),
            "cog_deg": round(d.get("cog", 0) / 100.0, 1),
            "heading_deg": None if hdg >= 511 else round(hdg, 1),
            "dest": d.get("dest"), "eta": d.get("eta"),
            "navistatus": NAVI_STATUS.get(d.get("navistatus"), "未知"),
            "last_report": _fmt_ts(d.get("lastdyn")),   # 数据截至时间(AIS最后上报)
            "_raw": d,
        }

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return ts


def _dms(v, pos, neg):
    """十进制度 → 官网度分格式 '度-分.3位方向'（如 30-35.008N），与 base.js latNe/lngNe 一致。"""
    if v is None:
        return ""
    hemi = pos if v >= 0 else neg
    v = abs(v)
    deg = int(v)
    minutes = (v - deg) * 60
    return f"{deg}-{minutes:06.3f}{hemi}"


# ─────────────────────────────────────────────────────────────────────────────
# 第 3 步：经纬度 → 结构化地址（国家/省/市/县区/镇/道路/POI）
# ─────────────────────────────────────────────────────────────────────────────
def _out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def wgs84_to_gcj02(lat, lon):
    """WGS-84 → GCJ-02，仅高德/腾讯需要；境外原样返回。"""
    if _out_of_china(lat, lon):
        return lat, lon
    a, ee = 6378245.0, 0.00669342162296594323
    x, y = lon - 105.0, lat - 35.0
    dlat = (-100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
            + (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
            + (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi)) * 2/3
            + (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30)) * 2/3)
    dlon = (300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
            + (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
            + (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi)) * 2/3
            + (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi)) * 2/3)
    radlat = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(radlat) ** 2
    sqm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqm) * math.pi)
    dlon = (dlon * 180.0) / (a / sqm * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def _s(x):
    """取有效字符串，空串/空列表/None → None。"""
    return x if isinstance(x, str) and x.strip() else None


try:
    from zhconv import convert as _zh_convert

    def _zhcn(s):
        """繁体/异体 → 简体中文（地址统一简体）。"""
        return _zh_convert(s, "zh-cn") if isinstance(s, str) and s else s
except Exception:
    def _zhcn(s):
        return s


def _norm(country=None, province=None, city=None, district=None, town=None,
          road=None, poi=None, postcode=None, provider=None, components=None,
          formatted=None, semantic=None):
    country, province, city = _zhcn(_s(country)), _zhcn(_s(province)), _zhcn(_s(city))
    district, town = _zhcn(_s(district)), _zhcn(_s(town))
    road, poi = _zhcn(_s(road)), _zhcn(_s(poi))
    postcode, formatted, semantic = _s(postcode), _zhcn(formatted), _zhcn(semantic)
    chain = []
    for v in (province, city, district, town, road, poi):
        if v and v not in chain:
            chain.append(v)
    fmt = formatted or " ".join(chain)
    if postcode and postcode not in (fmt or ""):
        fmt = f"{fmt} ({postcode})" if fmt else str(postcode)
    return {"provider": provider, "country": country, "province": province,
            "city": city, "district": district, "town": town,
            "road": road, "poi": poi, "postcode": postcode,
            "formatted": fmt or None, "semantic": semantic, "components": components or {}}


# --- 免 key 全球方案：Photon(OSM) + BigDataCloud 合并 -------------------------
def _photon_raw(lat, lon):
    r = cffi.get("https://photon.komoot.io/reverse",
                 params={"lat": lat, "lon": lon, "lang": "default"},
                 headers={"User-Agent": UA}, impersonate="chrome", timeout=15)
    feats = r.json().get("features") or []
    return feats[0].get("properties", {}) if feats else {}


def _bdc_raw(lat, lon):
    r = cffi.get("https://api.bigdatacloud.net/data/reverse-geocode-client",
                 params={"latitude": lat, "longitude": lon, "localityLanguage": "zh"},
                 timeout=15)
    return r.json()


def _geo_nokey(lat, lon, **_):
    p, b = {}, {}
    try:
        p = _photon_raw(lat, lon)
    except Exception:
        pass
    try:
        b = _bdc_raw(lat, lon)
    except Exception:
        pass
    if not p and not b:
        return None
    in_cn = not _out_of_china(lat, lon)
    admin = (b.get("localityInfo", {}) or {}).get("administrative", []) if b else []

    def blevel(*lvls):
        hit = [a.get("name") for a in admin if a.get("adminLevel") in lvls]
        return hit[-1] if hit else None

    country = _s(b.get("countryName")) or _s(p.get("country"))
    if in_cn:                              # 国内：Photon 是简体中文，优先
        province = _s(p.get("state")) or blevel(4)
        city = _s(b.get("city")) or _s(p.get("city"))
        district = _s(p.get("county")) or _s(p.get("district")) or blevel(6, 7)
        town = _s(p.get("locality")) or _s(p.get("district"))
    else:                                  # 国外：BigDataCloud 给中文行政区，Photon 给本地道路
        province = blevel(4) or _s(p.get("state"))
        city = _s(b.get("city")) or _s(p.get("city"))
        district = _s(p.get("county")) or _s(p.get("district")) or blevel(6, 7)
        town = _s(p.get("locality")) or _s(p.get("district")) or _s(b.get("locality"))
    road = _s(p.get("street"))
    name = _s(p.get("name"))
    poi = name if name and name != road else None
    if town and town == district:
        town = None
    if district and district == city:
        district = None
    res = _norm(country, province, city, district, town, road, poi,
                _s(p.get("postcode")), "photon+bigdatacloud",
                {"photon": p, "bdc_admin": admin})
    # 无任何陆地行政区/道路 → 判为近海/公海，统一给出海域名
    if not res["province"] and not res["city"] and not res["district"] and not res["road"]:
        info = (b.get("localityInfo", {}) or {}).get("informative", []) if b else []
        sea = res["town"] or _s(b.get("locality")) or (_s(info[0].get("name")) if info else None) or _s(country)
        return _norm(country="公海/国际水域" if not _s(country) else country,
                     province=sea, provider="photon+bigdatacloud",
                     formatted=(f"{sea}（海域·无陆地地址）" if sea else "公海（无陆地地址）"),
                     components={"photon": p, "bdc_admin": admin})
    return res


# --- 带 key 方案（更细：镇/街道/门牌） ---------------------------------------
def _geo_amap(lat, lon, key=None, **_):
    glat, glon = wgs84_to_gcj02(lat, lon)
    r = cffi.get("https://restapi.amap.com/v3/geocode/regeo",
                 params={"key": key, "location": f"{glon:.6f},{glat:.6f}",
                         "extensions": "all", "radius": 1000, "roadlevel": 0}, timeout=15)
    j = r.json()
    if j.get("status") != "1":
        raise RuntimeError(f"高德: {j.get('info')}")
    rc = j["regeocode"]
    c = rc.get("addressComponent", {})
    sn = c.get("streetNumber") or {}
    road = _s(sn.get("street"))
    poi = (f"{road}{_s(sn.get('number')) or ''}" if road else None)
    return _norm("中国", _s(c.get("province")), _s(c.get("city")) or _s(c.get("province")),
                 _s(c.get("district")), _s(c.get("township")), road, poi, None,
                 "amap", c, formatted=_s(rc.get("formatted_address")))


def _geo_baidu(lat, lon, key=None, **_):
    r = cffi.get("https://api.map.baidu.com/reverse_geocoding/v3/",
                 params={"ak": key, "output": "json", "coordtype": "wgs84ll",
                         "location": f"{lat:.6f},{lon:.6f}",
                         "extensions_poi": 1, "extensions_road": "true"}, timeout=15)
    j = r.json()
    if j.get("status") != 0:
        raise RuntimeError(f"百度: {j.get('message')}")
    res = j["result"]
    c = res.get("addressComponent", {})
    road = _s(c.get("street"))
    poi = (f"{road}{_s(c.get('street_number')) or ''}" if road else None)
    return _norm(_s(c.get("country")) or "中国", _s(c.get("province")), _s(c.get("city")),
                 _s(c.get("district")), _s(c.get("town")), road, poi, None,
                 "baidu", res, formatted=_s(res.get("formatted_address")),
                 semantic=_s(res.get("sematic_description")))


def _geo_google(lat, lon, key=None, **_):
    r = cffi.get("https://maps.googleapis.com/maps/api/geocode/json",
                 params={"latlng": f"{lat},{lon}", "key": key, "language": "zh-CN"}, timeout=15)
    j = r.json()
    if j.get("status") != "OK":
        raise RuntimeError(f"Google: {j.get('status')} {j.get('error_message','')}")
    res = j["results"][0]
    comps = res.get("address_components", [])

    def by(*types):
        for t in types:
            for c in comps:
                if t in c.get("types", []):
                    return c.get("long_name")
        return None

    return _norm(by("country"), by("administrative_area_level_1"),
                 by("locality", "administrative_area_level_2"),
                 by("administrative_area_level_2", "administrative_area_level_3"),
                 by("sublocality", "administrative_area_level_3", "neighborhood"),
                 by("route"),
                 (f"{by('route')}{by('street_number') or ''}" if by("route") else None),
                 by("postal_code"), "google", comps,
                 formatted=_s(res.get("formatted_address")))


_KEY_GEOCODERS = {"amap": _geo_amap, "baidu": _geo_baidu, "google": _geo_google}


def reverse_geocode(lat, lon, providers, keys=None):
    """按顺序尝试；带 key 的仅在提供 key 时启用；nokey=免key全球方案。"""
    keys = keys or {}
    errors = []
    for name in providers:
        try:
            if name == "nokey":
                res = _geo_nokey(lat, lon)
            elif name in _KEY_GEOCODERS:
                if not keys.get(name):
                    continue
                res = _KEY_GEOCODERS[name](lat, lon, key=keys.get(name))
            else:
                continue
            if res and (res.get("formatted") or res.get("province")):
                return res
        except Exception as e:
            errors.append(f"{name}:{e}")
    return {"provider": None, "formatted": None, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────────────────────────
def default_providers(keys):
    keys = keys or {}
    return [n for n in ("amap", "baidu", "google") if keys.get(n)] + ["nokey"]


def locate_one(loc, query, do_geocode=True, providers=None, keys=None):
    """单条查询：resolve→取位→逆编码。返回结果 dict（含 query_time / last_report）。"""
    providers = providers or default_providers(keys)
    item = {"query": str(query).strip(),
            "query_time": time.strftime("%Y-%m-%d %H:%M:%S")}   # 查询时间
    try:
        mmsi, _hit = resolve_mmsi(query)
        item["mmsi"] = mmsi
        item.update(loc.get_ship(mmsi))
        if do_geocode:
            item["location"] = reverse_geocode(item["lat"], item["lon"], providers, keys)
    except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"
    return item


def locate(queries, headless=True, do_geocode=True, providers=None, keys=None):
    providers = providers or default_providers(keys)
    results = []
    with ShipxyLocator(headless=headless) as loc:
        for q in queries:
            results.append(locate_one(loc, q, do_geocode, providers, keys))
    return results


def _print(item):
    if item.get("error"):
        print(f"\n[X] {item['query']}: {item['error']}")
        return
    print(f"\n[船] {item['query']}  ->  MMSI {item['mmsi']}  {item.get('name','')} {item.get('cnname','')}")
    print(f"   经纬度: {item['lat']}, {item['lon']}   航速{item['sog_knots']}节 航向{item['cog_deg']}度 状态:{item['navistatus']}")
    loc = item.get("location") or {}
    if loc.get("formatted"):
        c = loc.get("country") or ""
        print(f"   [位置] {c} {loc['formatted']}   来源:{loc['provider']}")
        seg = " / ".join(x for x in [loc.get("province"), loc.get("city"), loc.get("district"), loc.get("town")] if x)
        if seg:
            print(f"          省市县镇: {seg}")
    elif "location" in item:
        print(f"   [位置] 解析失败: {loc.get('errors')}")
    print(f"   目的地:{item.get('dest')}  数据截至:{item.get('last_report')}  查询时间:{item.get('query_time')}")


def main():
    ap = argparse.ArgumentParser(description="船名/MMSI → 船位经纬度 → 结构化地址")
    ap.add_argument("queries", nargs="+", help="船名或 MMSI，可多个")
    ap.add_argument("--show", action="store_true", help="有头浏览器(调试/首次过验证码)")
    ap.add_argument("--no-geocode", action="store_true")
    ap.add_argument("--providers", help="逆地理顺序，逗号分隔，如 amap,nokey")
    ap.add_argument("--amap-key", default=os.getenv("SHIPXY_AMAP_KEY"))
    ap.add_argument("--baidu-key", default=os.getenv("SHIPXY_BAIDU_KEY"))
    ap.add_argument("--google-key", default=os.getenv("SHIPXY_GOOGLE_KEY"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    keys = {"amap": args.amap_key, "baidu": args.baidu_key, "google": args.google_key}
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else default_providers(keys)

    results = locate(args.queries, headless=not args.show,
                     do_geocode=not args.no_geocode, providers=providers, keys=keys)
    for item in results:
        item.pop("_raw", None)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            _print(item)


if __name__ == "__main__":
    main()
