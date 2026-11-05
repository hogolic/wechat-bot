"""网易云音乐 weapi 客户端（移植自 Aurabot 插件的 src/song/netease.js）。

只做三件事：搜歌、拿下载地址、下载。
不登录只能下免费歌；VIP / 版权歌需要在 config.ini [netease] cookie 填 MUSIC_U。

命令行自测：
    python netease.py search 去年夏天
    python netease.py url 186016
    python netease.py download 186016 out.mp3
    python netease.py account
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PRESET_KEY = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b42"
    "4d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
EXPONENT = 0x10001
BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
API = "https://music.163.com/weapi"


class NeteaseError(Exception):
    pass


def _aes(text: str, key: bytes) -> str:
    data = text.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _rsa(sec_key: str) -> str:
    m = int.from_bytes(sec_key[::-1].encode("utf-8"), "big")
    return format(pow(m, EXPONENT, MODULUS), "x").rjust(256, "0")


def weapi(data: dict) -> dict:
    sec_key = "".join(random.choice(BASE62) for _ in range(16))
    return {
        "params": _aes(_aes(json.dumps(data, ensure_ascii=False), PRESET_KEY), sec_key.encode()),
        "encSecKey": _rsa(sec_key),
    }


def music_u(cookie: str | None) -> str:
    s = str(cookie or "").strip()
    if not s:
        return ""
    if "MUSIC_U=" in s:
        for part in s.split(";"):
            part = part.strip()
            if part.startswith("MUSIC_U="):
                return part[len("MUSIC_U="):].strip()
        return ""
    return "" if "=" in s else s


def _default_cookie() -> str:
    env = os.environ.get("NETEASE_COOKIE", "")
    if env:
        return env
    cfg = Path(__file__).with_name("config.ini")
    if cfg.is_file():
        import configparser

        cp = configparser.ConfigParser()
        cp.read(cfg, encoding="utf-8")
        if cp.has_section("netease"):
            return cp.get("netease", "cookie", fallback="")
    return ""


def post(path: str, data: dict, cookie: str = "", timeout: int = 20) -> dict:
    u = music_u(cookie)
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "referer": "https://music.163.com/",
        "user-agent": UA,
        "cookie": "os=pc; appver=3.0.0" + (f"; MUSIC_U={u}" if u else ""),
    }
    form = {**weapi({**data, "csrf_token": ""})}
    r = requests.post(API + path, data=form, headers=headers, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        raise NeteaseError(f"网易云返回了非 JSON（HTTP {r.status_code}）")
    code = j.get("code")
    if code != 200:
        msg = j.get("message") or j.get("msg") or ""
        raise NeteaseError(f"网易云返回 {code if code is not None else r.status_code}：{msg}")
    return j


def search_songs(keyword: str, cookie: str = "", limit: int = 10) -> list[dict]:
    j = post("/cloudsearch/pc", {"s": str(keyword), "type": 1, "limit": limit, "offset": 0}, cookie)
    out = []
    for s in (j.get("result") or {}).get("songs") or []:
        out.append({
            "id": s.get("id"),
            "name": s.get("name", ""),
            "alias": " ".join(s.get("alia") or []),
            "artists": " / ".join(a.get("name", "") for a in (s.get("ar") or [])),
            "durationSec": round((s.get("dt") or 0) / 1000),
            "vip": s.get("fee") in (1, 4),
        })
    return out


def song_url(song_id, cookie: str = "") -> dict:
    j = post(
        "/song/enhance/player/url/v1",
        {"ids": f"[{song_id}]", "level": "exhigh", "encodeType": "mp3"},
        cookie,
    )
    d = (j.get("data") or [{}])[0] or {}
    return {"url": d.get("url") or "", "trial": bool(d.get("freeTrialInfo")), "type": d.get("type") or "mp3"}


def account(cookie: str = "") -> dict:
    if not music_u(cookie):
        return {"ok": False, "message": "没填 Cookie"}
    try:
        j = post("/w/nuser/account/get", {}, cookie)
    except NeteaseError as exc:
        return {"ok": False, "message": str(exc)}
    profile = j.get("profile")
    if not profile:
        return {"ok": False, "message": "Cookie 失效了，重新复制一份"}
    return {
        "ok": True,
        "nickname": profile.get("nickname", ""),
        "vip": (j.get("account", {}).get("vipType") or 0) > 0,
    }


def download(url: str, dest, timeout: int = 120, max_bytes: int = 60 * 1024 * 1024) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers={"user-agent": UA, "referer": "https://music.163.com/"},
                      stream=True, timeout=timeout) as r:
        if not r.ok:
            raise NeteaseError(f"下载失败（HTTP {r.status_code}）")
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise NeteaseError("歌太大了（超过 60MB）")
                f.write(chunk)
    if dest.stat().st_size < 1024:
        raise NeteaseError("下载到的文件太小，可能不是音频")
    return dest


def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cookie = _default_cookie()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "search":
        for s in search_songs(" ".join(argv[1:]), cookie):
            print(json.dumps(s, ensure_ascii=False))
    elif cmd == "url":
        print(json.dumps(song_url(argv[1], cookie), ensure_ascii=False))
    elif cmd == "download":
        u = song_url(argv[1], cookie)
        if not u["url"] or u["trial"]:
            print("拿不到完整地址（可能需要会员或 cookie）")
            return 2
        print(download(u["url"], argv[2] if len(argv) > 2 else f"{argv[1]}.mp3"))
    elif cmd == "account":
        print(json.dumps(account(cookie), ensure_ascii=False))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
