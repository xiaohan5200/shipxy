# -*- coding: utf-8 -*-
"""
sign_reference.py  —  shipxy `s`/`t` 签名的纯 Python 还原（透明参考）

背景：
  shipxy 的 /ship/GetShip、/ship/GetShipm 等接口，会由前端 base.js 在每个 AJAX 前
  自动加两个请求头：
      s = 签名        t = 时间戳(秒)
  签名函数是 window.R0VOQ1NJR04（R0VOQ1NJR04 = base64("GENCSIGN")），
  定义在混淆的 Content/js/lib/elane.md5.min.js 里。反混淆后算法为：

      s = MD5( 排序后的参数串 + "&t=" + timestamp + 当日salt )
      t = floor(Date.now()/1000)

  - 排序参数串：body 的 key 按“小写字典序”排序后 `k=v` 用 `&` 连接（值保留原始大小写）
  - 当日 salt：按 UTC 日期 day%10 从内置 10 条表里取一条（每天轮换）

注意：
  真实取数不用这个签名——因为服务器还有网易易盾(设备指纹)反爬，纯 HTTP 即便签名正确也会
  被判 status:2。真正取数走 shipxy_locator.py 的浏览器方案。此文件仅用于：
    (1) 说明 S 参数到底是什么、怎么来的；
    (2) 万一将来站点去掉易盾，可配合 curl_cffi 直接用。

SALT 表来自 elane.md5.min.js?v=36，站点更新该文件时需重新提取（见文末说明）。
"""

import hashlib
import time
import json

# day%10 -> 当日 salt（已是抽掉固定位后的最终值）
SALT_TABLE = {
    0: "cc203d8f43c514ada8078f0db41",
    1: "a47c89688ce7f378bc71f8b83e5",
    2: "6adfe523c5d6ea55a73d70ef8de",
    3: "5a566f1910d4bf9495847122106",
    4: "a2edd2cc83f5a7dcfc2c0505ef4",
    5: "d81c0220c1be7ecd8fb8c843f59",
    6: "dd17e6b62f35c9da7a0da155b0e",
    7: "b9406bd442493bbd430479163cd",
    8: "cd5453f9d9ea1b9d8af4e1042d0",
    9: "9ee7b5a006f3511546f2cd33886",
}


def build_param_string(params: dict) -> str:
    """复刻站点 _0x36ded8：key 按小写字典序排序，k=v 用 & 连接。"""
    if not params:
        return ""
    keys = sorted(params.keys(), key=lambda k: k.lower())
    parts = []
    for k in keys:
        v = params[k]
        if isinstance(v, (list, dict)):        # 站点对数组/对象值会整体 JSON 化（此处忠实还原）
            v = json.dumps(params, separators=(",", ":"))
        parts.append(f"{k}={v}")
    return "&".join(parts)


def gen_sign(params: dict, ts: int = None):
    """返回 (s, t)。等价于浏览器里的 window.R0VOQ1NJR04(params)。"""
    ts = int(time.time()) if ts is None else int(ts)
    pstr = build_param_string(params)
    salt = SALT_TABLE[time.gmtime(ts).tm_mday % 10]
    signed = (pstr + "&" if pstr else "") + "t=" + str(ts) + salt
    return hashlib.md5(signed.encode("utf-8")).hexdigest(), ts


if __name__ == "__main__":
    mmsi = "413970005"
    s, t = gen_sign({"shipIDs": mmsi, "mmsi": mmsi})
    pstr = build_param_string({"shipIDs": mmsi, "mmsi": mmsi})
    salt = SALT_TABLE[time.gmtime(t).tm_mday % 10]
    print("参数串   :", pstr)
    print("签名原文 :", f"{pstr}&t={t}{salt}")
    print("s (sign) :", s)
    print("t (时间戳):", t)
    print()
    print("如需重新提取 SALT 表（站点更新 elane.md5.min.js 后）：")
    print("  1) 下载 https://www.shipxy.com/Content/js/lib/elane.md5.min.js")
    print("  2) node 里 eval 它拿到 window.R0VOQ1NJR04，")
    print("     mock Date.prototype.getUTCDate 返回 1..10，各算一次签名，")
    print("     从 MD5 入参尾部即可读出 10 条 salt（day%10 索引）。")
