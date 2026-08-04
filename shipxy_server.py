# -*- coding: utf-8 -*-
"""
shipxy_server.py — 船位定位 HTTP API（支持流式逐条返回）
启动: python shipxy_server.py [--port 8765]
"""
import sys, os, json, re, time, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import shipxy_locator as engine

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipxy_web.html")
_locator = None

# ── 拼音→中文映射 ──────────────────────────────────────────────────────────
try:
    from pinyin_city_map import PINYIN_CITY_MAP as _PINYIN_MAP
except ImportError:
    _PINYIN_MAP = {}

try:
    from pypinyin import pinyin as _pypinyin_fn, Style as _Style
    def _to_pinyin(name):
        return ''.join(p[0] for p in _pypinyin_fn(name, style=_Style.NORMAL)).upper()
except ImportError:
    _pypinyin_fn = None
    def _to_pinyin(name):
        return None


def _city_short(city):
    if not city:
        return ""
    for sfx in ("自治州", "地区", "林区", "特区", "市", "县", "区", "盟"):
        if city.endswith(sfx) and len(city) > len(sfx):
            return city[:-len(sfx)]
    return city


def _dest_chinese(dest):
    if not dest:
        return ""
    s = str(dest).strip()
    if re.search(r'[一-鿿]', s):
        m = re.findall(r'[一-鿿]+', s)
        return m[0] if m else s
    first = re.split(r'[,/]', s)[0].strip()
    candidates = []
    raw = re.sub(r'[^A-Za-z]', '', first).upper()
    if raw:
        candidates.append(raw)
    nospace = re.sub(r'\s+', '', first).upper()
    if nospace and nospace != raw:
        candidates.append(nospace)
    parts = first.split()
    if parts:
        first_word = parts[0].upper()
        if first_word not in candidates:
            candidates.append(first_word)
    for cand in candidates:
        if cand in _PINYIN_MAP:
            return _PINYIN_MAP[cand]
    try:
        from difflib import get_close_matches
        for cand in candidates:
            if len(cand) >= 3:
                matches = get_close_matches(cand, _PINYIN_MAP.keys(), n=1, cutoff=0.78)
                if matches:
                    return _PINYIN_MAP[matches[0]]
    except ImportError:
        pass
    if _pypinyin_fn:
        try:
            for cand in candidates:
                py = _to_pinyin(cand)
                if py and py in _PINYIN_MAP:
                    return _PINYIN_MAP[py]
        except Exception:
            pass
    return first


def _make_path(city, dest):
    c = _city_short(city) if city else ""
    d = _dest_chinese(dest) if dest else ""
    if c and d:
        return f"{c}-{d}"
    return c or d or ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}", file=sys.stderr, flush=True)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json({})

    def do_GET(self):
        global _locator
        p = urlparse(self.path)
        path = p.path.rstrip("/") or "/"
        qs = parse_qs(p.query)

        if path in ("/", "/index.html"):
            try:
                with open(_HTML_PATH, "r", encoding="utf-8") as f:
                    return self._html(f.read())
            except FileNotFoundError:
                return self._json({"error": "shipxy_web.html not found"}, 404)

        if path == "/api/health":
            return self._json({"status": "ok", "locator_ready": _locator is not None})

        if path == "/api/search":
            kw = (qs.get("kw") or [""])[0].strip()
            if not kw:
                return self._json({"error": "missing kw"}, 400)
            try:
                ships = engine.search_ship(kw)
                return self._json({"ships": ships})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path == "/api/geocode":
            try:
                lat = float((qs.get("lat") or [0])[0])
                lon = float((qs.get("lon") or [0])[0])
            except (ValueError, TypeError):
                return self._json({"error": "missing lat/lon"}, 400)
            amap_key = (qs.get("amap_key") or [None])[0]
            keys = {"amap": amap_key, "baidu": None, "google": None}
            providers = engine.default_providers(keys)
            try:
                result = engine.reverse_geocode(lat, lon, providers, keys)
                return self._json(result)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path == "/api/locator/close":
            try:
                if _locator:
                    _locator.__exit__()
                    _locator = None
                return self._json({"status": "closed"})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "Not Found"}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path.rstrip("/")

        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return self._json({"error": "JSON parse error"}, 400)

        if path in ("/api/locate", "/api/locate/stream"):
            return self._handle_locate_stream(body)

        return self._json({"error": "Not Found"}, 404)

    def _handle_locate_stream(self, body):
        """流式逐条返回：NDJSON"""
        global _locator
        queries = body.get("queries", [])
        if not queries:
            return self._json({"error": "missing queries"}, 400)

        amap_key = body.get("amap_key") or None
        headless = body.get("headless", True)
        keys = {"amap": amap_key, "baidu": None, "google": None}
        providers = engine.default_providers(keys)

        # 打开浏览器会话（首次）
        if _locator is None:
            try:
                print("[server] opening browser session...", file=sys.stderr, flush=True)
                _locator = engine.ShipxyLocator(headless=headless)
                _locator.__enter__()
                print("[server] browser ready", file=sys.stderr, flush=True)
            except Exception as e:
                self._json({"error": f"browser start failed: {e}"}, 500)
                return

        # 流式响应头
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        def flatten(item, idx):
            loc = item.get("location") or {}
            return {
                "idx": idx,
                "query": item.get("query", ""),
                "name": item.get("name", "") or "",
                "cnname": item.get("cnname", "") or "",
                "mmsi": item.get("mmsi", ""),
                "lat": item.get("lat", ""),
                "lon": item.get("lon", ""),
                "pos_dms": f"{item.get('lat_dms', '')}  {item.get('lon_dms', '')}".strip(),
                "sog_knots": item.get("sog_knots", ""),
                "cog_deg": item.get("cog_deg", ""),
                "navistatus": item.get("navistatus", ""),
                "country": loc.get("country", "") or "",
                "province": loc.get("province", "") or "",
                "city": loc.get("city", "") or "",
                "district": loc.get("district", "") or "",
                "town": loc.get("town", "") or "",
                "address": loc.get("formatted", "") or "",
                "dest": item.get("dest", "") or "",
                "path": _make_path(loc.get("city", ""), item.get("dest", "")),
                "last_report": item.get("last_report", "") or "",
                "query_time": item.get("query_time", ""),
                "error": item.get("error", "") or "",
            }

        total = len(queries)
        emit({"type": "start", "total": total})

        for i, q in enumerate(queries):
            try:
                item = engine.locate_one(_locator, q, True, providers, keys)
                row = flatten(item, i + 1)
                row["type"] = "row"
                emit(row)
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                # 浏览器会话失效 → 尝试恢复
                if "browser" in str(e).lower() or "page" in str(e).lower() or "context" in str(e).lower() or "closed" in str(e).lower():
                    try:
                        print(f"[server] browser session broken, restarting... ({err_msg})", file=sys.stderr, flush=True)
                        if _locator:
                            try: _locator.__exit__(None, None, None)
                            except Exception: pass
                        _locator = engine.ShipxyLocator(headless=headless)
                        _locator.__enter__()
                        # 重试本次查询
                        item = engine.locate_one(_locator, q, True, providers, keys)
                        row = flatten(item, i + 1)
                        row["type"] = "row"
                        emit(row)
                        continue
                    except Exception as e2:
                        err_msg = f"{type(e2).__name__}: {e2}"
                emit({
                    "type": "row",
                    "idx": i + 1,
                    "query": str(q).strip(),
                    "query_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "error": err_msg,
                    "name": "", "cnname": "", "mmsi": "", "lat": "", "lon": "",
                    "pos_dms": "", "sog_knots": "", "cog_deg": "", "navistatus": "",
                    "country": "", "province": "", "city": "", "district": "",
                    "town": "", "address": "", "dest": "", "path": "", "last_report": "",
                })

        emit({"type": "done", "total": total})


def main():
    ap = argparse.ArgumentParser(description="ShipXY API Server (streaming)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = HTTPServer((args.host, args.port), Handler)
    print(f"\n  ShipXY API Server @ http://{args.host}:{args.port}")
    print(f"  Health: http://{args.host}:{args.port}/api/health")
    print(f"  Press Ctrl+C to stop\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if _locator:
            try:
                _locator.__exit__()
            except Exception:
                pass
        server.shutdown()
        print("\nstopped.", flush=True)


if __name__ == "__main__":
    main()
