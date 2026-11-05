#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeepSeek × 微信 自动回复机器人

原理：用 wechatauto（微信 4.x 自动化库）读本地数据库拿新消息，
调 DeepSeek（OpenAI 兼容接口）生成回复，再驱动微信窗口把回复发出去。

常用命令
    python bot.py                 启动机器人
    python bot.py --check         环境自检（配置 / API / 微信数据库 / 监听对象）
    python bot.py --ask "你好"     只测试 DeepSeek 是否连通，不碰微信
    python bot.py --sessions      列出微信会话（查看内部 ID / 显示名）
    python bot.py --dry-run       跑监听但不真的发送，回复打印在终端

注意：读取消息需要微信已登录（密钥在进程内存里），发送需要微信窗口
可见且桌面未锁屏。
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

import netease
import memory_bridge

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = Path(os.environ.get("WECHAT_BOT_TOOLS", BASE_DIR / "tools"))
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.ini"

for _d in (DATA_DIR, HISTORY_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Windows 控制台默认 GBK，中文/emoji 会炸；统一成 UTF-8
if os.name == "nt":
    try:
        os.system("chcp 65001 >nul 2>&1")
    except Exception:
        pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging

log = logging.getLogger("bot")
_CRASH_FILE = None


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)

    try:
        fh = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except OSError:
        pass

    log.propagate = False

    # wechatauto 的监听线程出错只写 stderr，日志里看不到；劫持一下写进日志
    class _StderrToLog(io.TextIOBase):
        def write(self, s: str) -> int:
            s = (s or "").rstrip("\n")
            if s.strip():
                log.warning("[wechatauto] %s", s)
            return len(s)

        def flush(self) -> None:
            pass

    sys.stderr = _StderrToLog()


def quiet_third_party() -> None:
    """wechatauto 的日志比较吵（还会接管 root logger），压到 WARNING。"""
    try:
        logging.getLogger("wechatauto").setLevel(logging.WARNING)
    except Exception:
        pass
    try:
        from wechatauto import WxParam

        WxParam.ENABLE_FILE_LOGGER = False  # 别在项目里再生成 wechatauto_logs
    except Exception:
        pass


def ensure_pid_detection() -> None:
    """wechatauto 用 tasklist 找微信进程；某些权限/杀软环境读不到，
    这里补一个 psutil 兜底（tasklist 有结果时行为不变）。"""
    try:
        from wechatauto import db as wxdb
    except Exception:
        return
    if getattr(wxdb.WeChatDB._find_weixin_pids, "_dsh_patched", False):
        return

    original = wxdb.WeChatDB._find_weixin_pids

    def find(self):
        pids = original(self)
        if pids:
            return pids
        try:
            import psutil

            return [
                p.pid for p in psutil.process_iter(["name"])
                if (p.info.get("name") or "").lower() == "weixin.exe"
            ]
        except Exception:
            return []

    find._dsh_patched = True
    wxdb.WeChatDB._find_weixin_pids = find


def acquire_single_instance():
    """同一时间只允许一个机器人进程（双开会抢微信窗口、丢回复）。"""
    lock_path = DATA_DIR / "bot.lock"
    try:
        fh = open(lock_path, "a+")
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    pass


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        env_path = os.environ.get("DSWECHAT_CONFIG")
        if env_path:
            path = Path(env_path)
        cp = configparser.ConfigParser(interpolation=None, allow_no_value=False)
        cp.optionxform = str
        if not cp.read(path, encoding="utf-8"):
            raise ConfigError(
                f"读不到配置文件：{path}\n"
                f"请确认 config.ini 和 bot.py 在同一个文件夹里。"
            )
        self.cp = cp

    def get(self, section: str, key: str, default: str = "") -> str:
        try:
            v = self.cp.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
        return default if v is None else v.strip()

    def get_int(self, section: str, key: str, default: int) -> int:
        raw = self.get(section, key, "")
        if not raw:
            return default
        try:
            return int(float(raw))
        except ValueError:
            log.warning("配置 %s.%s=%r 不是数字，用默认值 %s", section, key, raw, default)
            return default

    def get_float(self, section: str, key: str, default: float) -> float:
        raw = self.get(section, key, "")
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            log.warning("配置 %s.%s=%r 不是数字，用默认值 %s", section, key, raw, default)
            return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        raw = self.get(section, key, "")
        if not raw:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on", "y", "是", "开")

    def get_list(self, section: str, key: str) -> list[str]:
        raw = self.get(section, key, "")
        if not raw:
            return []
        for sep in ("\n", "，", ";", "；"):
            raw = raw.replace(sep, ",")
        return [x.strip() for x in raw.split(",") if x.strip()]


# ---------------------------------------------------------------------------
# DeepSeek 客户端
# ---------------------------------------------------------------------------
class LLMError(Exception):
    pass


class DeepSeekClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 1.3, max_tokens: int = 2000,
                 timeout: float = 120):
        import requests

        self.session = requests.Session()
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/chat/completions"
        self.api_key = api_key.strip()
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def _post_once(self, payload: dict, headers: dict):
        """发一次请求，带硬超时。

        硅基流动的 keep-alive 连接偶尔会假死：TCP 还 Established，但响应永远
        不回来，requests 的 timeout 管不到这种半死连接。所以这里一律不复用连接，
        再用一个看门狗线程兜底，到点直接判超时，绝不阻塞监听工作线程。
        """
        import requests

        result: dict = {}

        def worker():
            try:
                result["resp"] = requests.post(
                    self.url, headers=headers, json=payload,
                    timeout=(10, self.timeout),
                )
            except Exception as exc:
                result["err"] = exc

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(self.timeout + 15)
        if th.is_alive():
            raise LLMError(
                f"请求 {self.base_url} 超过 {self.timeout:.0f}s 没有响应（连接假死），已放弃"
            )
        if "err" in result:
            raise result["err"]
        return result["resp"]

    def complete(self, messages: list[dict], max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = "未知错误"
        for attempt in (1, 2):
            try:
                resp = self._post_once(payload, headers)
            except LLMError:
                raise
            except Exception as exc:  # 连接失败 / 超时
                last_err = f"连不上 {self.base_url}（{exc.__class__.__name__}: {exc}）"
                if attempt == 1:
                    time.sleep(2)
                    continue
                raise LLMError(last_err + "\n检查网络，或把 base_url 换成官方/硅基流动地址。")

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    msg = data["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                except Exception:
                    raise LLMError(f"接口返回格式看不懂：{resp.text[:300]}")
                if not content:
                    raise LLMError("模型返回了空内容（可能被内容审核拦了）")
                return content

            body = resp.text[:300]
            if resp.status_code in (401, 403):
                raise LLMError(
                    f"API Key 无效或无权限（HTTP {resp.status_code}）。\n"
                    f"请检查 config.ini 里的 api_key 是否复制完整。\n{body}"
                )
            if resp.status_code == 402 or "insufficient" in body.lower() or "balance" in body.lower():
                raise LLMError(
                    f"账户余额不足（HTTP {resp.status_code}）。\n"
                    f"硅基流动新账号有免费额度，用完后需要充值。\n{body}"
                )
            if resp.status_code == 404:
                raise LLMError(
                    f"接口或模型不存在（HTTP 404）：\n  {self.url}\n  model = {self.model}\n"
                    f"检查 base_url 结尾是否有 /v1，模型名是否拼对。\n{body}"
                )
            if resp.status_code == 429:
                last_err = "请求太频繁（HTTP 429），稍后再试"
            elif 400 <= resp.status_code < 500:
                raise LLMError(f"请求被拒绝（HTTP {resp.status_code}）：{body}")
            else:
                last_err = f"服务端错误（HTTP {resp.status_code}）：{body}"

            if attempt == 1:
                time.sleep(3)

        raise LLMError(last_err)


# ---------------------------------------------------------------------------
# 对话历史
# ---------------------------------------------------------------------------
def _write_json(path: Path, data) -> None:
    """原子写；沙箱里 rename 可能被拒，退回直接写。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("保存历史失败：%s", exc)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class ChatHistory:
    """按聊天分别保存上下文，重启不丢。"""

    def __init__(self, max_messages: int):
        self.max_messages = max(0, int(max_messages))
        self._lock = threading.Lock()

    def _path(self, chat_id: str) -> Path:
        h = hashlib.md5(chat_id.encode("utf-8")).hexdigest()[:16]
        return HISTORY_DIR / f"{h}.json"

    def load(self, chat_id: str) -> list[dict]:
        if not self.max_messages:
            return []
        try:
            data = json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        msgs = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(msgs, list):
            return []
        return [m for m in msgs if isinstance(m, dict) and m.get("role") in ("user", "assistant")]

    def append(self, chat_id: str, role: str, content: str) -> None:
        if not self.max_messages:
            return
        with self._lock:
            msgs = self.load(chat_id)
            msgs.append({"role": role, "content": content})
            msgs = msgs[-self.max_messages:]
            _write_json(self._path(chat_id), {
                "chat_id": chat_id,
                "messages": msgs,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })

    def reset(self, chat_id: str) -> None:
        with self._lock:
            try:
                self._path(chat_id).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 群聊主动接话
# ---------------------------------------------------------------------------
PROACTIVE_RULES = (
    "现在是群聊主动模式：没有人 @ 你，接不接话完全由你决定。\n"
    "这条规则覆盖人设里「没人 @ 就安静泡着」的默认设定，但只在真的想接时接。\n"
    "如果接：\n"
    "- 1~2 句，总共不超过 {max_chars} 字，像群友随口插话。\n"
    "- 不要 @ 任何人；不要总结、列点、讲道理；不要用「作为 AI」「希望能帮到你」这类腔调。\n"
    "- 不要重复别人刚说过的话，也不要每句都接。\n"
    "- 只输出要发的内容；不想接就只输出 [SKIP]。"
)


class ProactiveState:
    """群聊「没被 @ 也可能接一句」的冷却、限额和最近消息缓存。"""

    MODES = {
        "quiet":    {"probability": 0.05, "min_interval": 600, "min_messages": 12, "daily_max": 6},
        "balanced": {"probability": 0.12, "min_interval": 240, "min_messages": 6,  "daily_max": 20},
        "lively":   {"probability": 0.25, "min_interval": 90,  "min_messages": 3,  "daily_max": 40},
    }

    def __init__(self, cfg: Config):
        self.enabled = cfg.get_bool("proactive", "enabled", False)
        self.groups = set(cfg.get_list("proactive", "groups"))
        mode = (cfg.get("proactive", "mode", "balanced") or "balanced").lower()
        self.mode = mode if mode in self.MODES else "balanced"
        base = self.MODES[self.mode]
        self.probability = cfg.get_float("proactive", "probability", base["probability"])
        self.keyword_probability = cfg.get_float("proactive", "keyword_probability", 0.35)
        self.min_interval = cfg.get_float("proactive", "min_interval", base["min_interval"])
        self.min_messages = cfg.get_int("proactive", "min_messages", base["min_messages"])
        self.daily_max = cfg.get_int("proactive", "daily_max", base["daily_max"])
        self.min_speakers = cfg.get_int("proactive", "min_speakers", 2)
        self.max_chars = cfg.get_int("proactive", "max_chars", 60)
        self.active_hours = cfg.get("proactive", "active_hours", "9-23")
        self.keywords = cfg.get_list("proactive", "keywords")

        self._lock = threading.Lock()
        self._recent: dict[str, deque] = {}
        self._since_bot: dict[str, int] = {}
        self._speakers: dict[str, dict[str, float]] = {}
        self._counts: dict[str, int] = {}
        self._last_sent: dict[str, float] = {}
        self._day = self._today()
        self._load()

    def _path(self) -> Path:
        return DATA_DIR / "proactive.json"

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("date") != self._day:
            return
        counts = data.get("counts")
        if isinstance(counts, dict):
            self._counts = {str(k): int(v) for k, v in counts.items()
                            if isinstance(v, (int, float))}
        last = data.get("last")
        if isinstance(last, dict):
            self._last_sent = {str(k): float(v) for k, v in last.items()
                               if isinstance(v, (int, float))}

    def _save(self) -> None:
        _write_json(self._path(), {
            "date": self._day,
            "counts": self._counts,
            "last": self._last_sent,
        })

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._counts.clear()
            self._last_sent.clear()

    def note_message(self, chat_id: str, sender: str, text: str) -> None:
        with self._lock:
            self._recent.setdefault(chat_id, deque(maxlen=12)).append(
                (time.time(), sender or "群友", text)
            )
            self._since_bot[chat_id] = self._since_bot.get(chat_id, 0) + 1
            self._speakers.setdefault(chat_id, {})[sender or "群友"] = time.time()

    def note_bot_reply(self, chat_id: str) -> None:
        with self._lock:
            self._since_bot[chat_id] = 0
            self._speakers.get(chat_id, {}).clear()

    def recent(self, chat_id: str) -> list[tuple[float, str, str]]:
        with self._lock:
            return list(self._recent.get(chat_id, ()))

    def _in_hours(self) -> bool:
        try:
            start_s, end_s = self.active_hours.split("-", 1)
            start, end = int(float(start_s)), int(float(end_s))
            if start == end:
                return True
            hour = datetime.now().hour
            if start < end:
                return start <= hour < end
            return hour >= start or hour < end
        except (ValueError, AttributeError):
            return True

    def consider(self, chat_id: str, group_name: str, text: str) -> bool:
        """过滤 + 掷骰子。命中才值得花一次模型调用去判断要不要接。"""
        with self._lock:
            if not self.enabled or not self.groups:
                return False
            if group_name not in self.groups:
                return False
            if not self._in_hours():
                return False
            if len(text.strip()) < 2 or text.lstrip().startswith("/"):
                return False
            # 这条消息 @ 了别人，是在点名，不插话
            if "@" in text:
                return False
            self._roll_day()
            if self._counts.get(chat_id, 0) >= self.daily_max:
                return False
            now = time.time()
            if now - self._last_sent.get(chat_id, 0.0) < self.min_interval:
                return False
            if self._since_bot.get(chat_id, 0) < self.min_messages:
                return False
            active = [s for s, t in self._speakers.get(chat_id, {}).items()
                      if now - t <= 600]
            if len(set(active)) < self.min_speakers:
                return False
            p = self.probability
            if any(k and k in text for k in self.keywords):
                p = max(p, self.keyword_probability)
            return random.random() < p

    def note_sent(self, chat_id: str) -> None:
        with self._lock:
            self._roll_day()
            self._counts[chat_id] = self._counts.get(chat_id, 0) + 1
            self._last_sent[chat_id] = time.time()
            self._save()


# ---------------------------------------------------------------------------
# 文本切分
# ---------------------------------------------------------------------------
def split_text(text: str, limit: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n"), window.rfind("。"), window.rfind("！"), window.rfind("？"))
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# 微信纯文本化
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.S)
_UNCLOSED_FENCE_RE = re.compile(r"^[ \t]*```[^\n]*$", re.M)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_BOLD_RE = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_STRIKE_RE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)


def _plain_inline(text: str) -> str:
    """清理行内的常见 Markdown 标记（不碰代码块）。"""
    text = _LINK_RE.sub(r"\1（\2）", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _HR_RE.sub("", text)
    return text


def to_wechat_text(text: str) -> str:
    """把模型回复里的 Markdown 转成微信里能直接看的纯文本。

    微信不渲染 Markdown：代码围栏会原样显示成 ```，**加粗** 也会露出来。
    这里拆掉围栏、保留代码原文，只清理正文里的常见行内标记。
    代码块内部一个字都不改，避免误伤 * / _ / ` 这类合法字符。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        out.append(_plain_inline(text[pos:m.start()]))
        out.append(m.group(1).strip("\n"))
        pos = m.end()
    tail = _UNCLOSED_FENCE_RE.sub("", text[pos:])
    out.append(_plain_inline(tail))
    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 机器人
# ---------------------------------------------------------------------------
class BotError(Exception):
    pass


class WeChatBot:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.name = cfg.get("bot", "name", "小D") or "小D"
        self.system_prompt = self._load_persona()

        self.reply_groups = cfg.get_bool("wechat", "reply_groups", False)
        self.trigger = (cfg.get("wechat", "trigger", "all") or "all").lower()
        self.trigger_prefix = cfg.get("wechat", "trigger_prefix", "")
        self.send_verify = cfg.get_bool("wechat", "send_verify", False)
        self.reply_delay = cfg.get_float("wechat", "reply_delay", 1.0)
        self.split_limit = cfg.get_int("wechat", "split_limit", 900)
        self.plain_text = cfg.get_bool("wechat", "plain_text", True)
        self.error_reply = cfg.get_bool("wechat", "error_reply", True)
        self.listen_all = cfg.get_bool("wechat", "listen_all", False)
        self.listen_groups = cfg.get_bool("wechat", "listen_groups", False)
        self.listen_names = cfg.get_list("wechat", "listen_list")
        self.at_names = [a for a in cfg.get_list("wechat", "at_names") if a]

        self.api = DeepSeekClient(
            base_url=cfg.get("deepseek", "base_url", ""),
            api_key=cfg.get("deepseek", "api_key", ""),
            model=cfg.get("deepseek", "model", "deepseek-chat"),
            temperature=cfg.get_float("deepseek", "temperature", 1.3),
            max_tokens=cfg.get_int("deepseek", "max_tokens", 2000),
            timeout=cfg.get_float("deepseek", "timeout", 120),
        )
        self.history = ChatHistory(cfg.get_int("bot", "max_history", 16))
        self.proactive = ProactiveState(cfg)
        self.voice_enabled = cfg.get_bool("voice", "enabled", True)
        self.voice_name = cfg.get("voice", "voice", "zh-CN-XiaoxiaoNeural")
        self.voice_rate = cfg.get("voice", "rate", "")
        self.voice_pitch = cfg.get("voice", "pitch", "")
        self.voice_max_chars = cfg.get_int("voice", "max_chars", 200)
        self.sing_enabled = cfg.get_bool("sing", "enabled", False)
        self.sing_ref = cfg.get("sing", "ref", str(TOOLS_DIR / "data" / "refs" / "ref.wav"))
        self.sing_songs_dir = Path(cfg.get("sing", "songs_dir", str(TOOLS_DIR / "data" / "songs")))
        self.sing_default = cfg.get("sing", "default", "")
        self.sing_send_mode = (cfg.get("sing", "send_mode", "split_voice") or "split_voice").lower()
        self.sing_max_segment = cfg.get_float("sing", "max_segment", 19.0)
        self.sing_steps = cfg.get_int("sing", "diffusion_steps", 30)
        self.sing_with_music = cfg.get_bool("sing", "with_music", False)
        self.sing_inbox = Path(cfg.get("sing", "inbox_dir", str(TOOLS_DIR / "data" / "music_inbox")))
        self.sing_auto_ingest = cfg.get_bool("sing", "auto_ingest", True)
        self.sing_ingest_interval = max(5, cfg.get_int("sing", "ingest_interval", 20))
        self.netease_enabled = cfg.get_bool("netease", "enabled", False)
        self.netease_cookie = cfg.get("netease", "cookie", "")
        self.netease_limit = max(3, cfg.get_int("netease", "search_limit", 10))
        self.sing_max_seconds = max(0.0, cfg.get_float("sing", "max_seconds", 60.0))
        self.sing_clip_start = (cfg.get("sing", "clip_start", "auto") or "auto").lower()
        self.memory_enabled = cfg.get_bool("memory", "enabled", False)
        self.memory_chats = {c for c in cfg.get_list("memory", "chats") if c}
        self.memory = memory_bridge.MemoryStore(
            cfg.get("memory", "file", ""),
            cfg.get("memory", "owner_qq", ""),
            cfg.get_int("memory", "top_k", 6),
        )
        self._sing_lock = threading.Lock()
        self._fetch_lock = threading.Lock()

        self.db = None
        self.gui = None
        self._gui_lock = threading.Lock()
        self.listener = None
        self.self_wxid = ""
        self.self_name = ""
        self.targets: dict[str, str] = {}   # username -> 显示名
        self._seen: set = set()
        self._stop = threading.Event()

    # -- 启动准备 ---------------------------------------------------------
    def _load_persona(self) -> str:
        p = self.cfg.get("persona", "system_file", "")
        if p:
            fp = Path(p)
            if not fp.is_absolute():
                fp = BASE_DIR / fp
            try:
                text = fp.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                log.warning("人设文件不存在：%s（改用内联 system）", fp)
        inline = self.cfg.get("persona", "system", "")
        if inline:
            return inline
        return f"你是「{self.name}」，一个通过 DeepSeek 接入微信的 AI 助手。回答简短、自然、口语化。"

    def validate_config(self) -> None:
        if not self.api.api_key:
            raise ConfigError(
                "还没填 api_key。\n"
                "打开 config.ini，把 [deepseek] 下面的 api_key 填成你的 Key（sk- 开头）。\n"
                "硅基流动：登录 cloud.siliconflow.cn → API 密钥 → 新建/复制。"
            )
        if not self.api.base_url.startswith("http"):
            raise ConfigError("base_url 必须以 http:// 或 https:// 开头，例如 https://api.siliconflow.cn/v1")
        if not (self.listen_all or self.listen_groups or self.listen_names):
            raise ConfigError(
                "没配置要监听的聊天：\n"
                "  填 listen_list（聊天名），或者把 listen_groups 设成 true（监听所有群）。"
            )
        if self.listen_groups and not self.reply_groups:
            log.warning("listen_groups = true 但 reply_groups = false：群消息会被忽略。")
        if self.proactive.enabled and not self.proactive.groups:
            log.warning("proactive.enabled = true 但 proactive.groups 为空：不会主动接话。")
        if self.proactive.enabled and not self.listen_groups and not self.listen_names:
            log.warning("proactive 已开启，但没有监听任何群；先配置 listen_list 或 listen_groups。")

    def connect(self) -> None:
        from wechatauto import WeChatDB

        ensure_pid_detection()
        log.info("正在读取微信数据库（首次需要几十秒解密）…")
        try:
            self.db = WeChatDB(workdir=str(CACHE_DIR))
        except RuntimeError as exc:
            raise BotError(
                f"读取微信数据库失败：{exc}\n"
                f"请确认：\n"
                f"  1) 微信（Weixin）已启动并登录，且没有锁屏/退出；\n"
                f"  2) 微信版本是 4.1.12 及以上（本机检测到 4.x 即可）；\n"
                f"  3) 用同一个 Windows 账户运行本程序；\n"
                f"  4) 如果微信是以管理员身份运行的，本程序也要右键「以管理员身份运行」。"
            ) from exc

        info = {}
        try:
            info = self.db.get_self_info() or {}
        except Exception as exc:
            log.debug("get_self_info 失败：%s", exc)
        self.self_wxid = info.get("username") or getattr(self.db, "wxid", "") or ""
        self.self_name = info.get("nick_name") or info.get("remark") or ""
        log.info("已登录账号：%s%s", self.self_name or "(未知)", f"（{self.self_wxid}）" if self.self_wxid else "")

    def resolve_targets(self) -> dict[str, str]:
        """把配置里的昵称/备注解析成 username，并记住发送时用的显示名。"""
        sessions = self.db.get_sessions(limit=500)
        by_user = {s["username"]: s for s in sessions}

        resolved: dict[str, str] = {}
        for raw in self.listen_names:
            if raw in by_user:
                resolved[raw] = self.db.get_nickname(raw)
                continue
            try:
                hits = self.db.search_contact(raw)
            except Exception:
                hits = []
            exact = [h for h in hits if raw in (h.get("nick_name"), h.get("remark"))]
            pick = (exact or hits or [None])[0]
            if pick:
                uname = pick["username"]
                resolved[uname] = self.db.get_nickname(uname) or pick.get("remark") or pick.get("nick_name") or raw
                if uname != raw:
                    log.info("  「%s」→ %s（%s）", raw, resolved[uname], uname)
            else:
                log.warning("  ⚠ 找不到聊天「%s」，先跳过。可以先运行 python bot.py --sessions 看看有哪些会话。", raw)

        if self.listen_groups:
            groups = [s for s in sessions if s["username"].endswith("@chatroom")]
            for s in groups:
                uname = s["username"]
                if uname not in resolved:
                    resolved[uname] = self.db.get_nickname(uname) or uname
                    log.info("  + 自动监听群：%s", resolved[uname])
            if not groups:
                log.warning("  listen_groups = true，但当前微信里还没有任何群；被拉进群后重启一次即可。")

        if not resolved and not (self.listen_all or self.listen_groups):
            raise BotError(
                "listen_list 里的聊天一个都没匹配上。\n"
                "运行 python bot.py --sessions 查看微信里实际的昵称，再填进 config.ini。"
            )
        return resolved

    def warmup_gui(self) -> None:
        if self.dry_run:
            return
        try:
            with self._gui_lock:
                self._ensure_gui()
            log.info("发送通道：就绪（微信窗口已连接）")
        except Exception as exc:
            log.warning("发送通道暂未就绪：%s（收到消息时会再试）", exc)

    def _ensure_gui(self):
        if self.gui is None:
            from wechatauto import WeChatGUI

            log.info("正在连接微信窗口（首次会做一次界面校准，微信可能闪一下）…")
            self.gui = WeChatGUI()
        return self.gui

    # -- 收到消息 ---------------------------------------------------------
    def _make_callback(self, chat_id: str):
        def cb(msg: dict, lst) -> None:
            try:
                self.handle(chat_id, msg)
            except Exception:
                log.error("处理消息出错：\n%s", traceback.format_exc())

        return cb

    def _is_self(self, msg: dict) -> bool:
        if self.self_wxid and msg.get("sender_username") == self.self_wxid:
            return True
        # 兜底：老版本库里自己发的消息 sender_username 可能为空
        return False

    def _at_aliases(self) -> list[str]:
        """群里可能被 @ 到的名字：配置的别名 + 机器人名 + 微信昵称。"""
        names = list(self.at_names)
        if self.name and self.name not in names:
            names.append(self.name)
        if self.self_name and self.self_name not in names:
            names.append(self.self_name)
        return names

    def _should_reply(self, chat_id: str, text: str, sender_name: str = "",
                      sender_id: str = "") -> tuple[bool, str]:
        """判断这条消息要不要回；要回则返回清理后的正文。"""
        is_group = chat_id.endswith("@chatroom")

        if is_group:
            if not self.reply_groups:
                return False, ""
            # 群消息正文可能带「发送者:\n」前缀（有时是昵称，有时是 wxid），先去掉
            for mark in (sender_name, sender_id):
                if not mark:
                    continue
                for sep in (":", "："):
                    prefix = mark + sep
                    if text.startswith(prefix):
                        text = text[len(prefix):].lstrip("\n ").strip()
                        break
                else:
                    continue
                break
            # 被 @ 才回
            for alias in self._at_aliases():
                token = f"@{alias}"
                if token in text:
                    text = text.replace(token, " ")
                    # 微信 @ 后常带特殊空白
                    text = text.replace("\u2005", " ").replace("\u200b", " ").strip()
                    return bool(text), text
            # 兼容「前缀触发」（群里想喊它也可以直接打前缀）
            if self.trigger == "prefix" and self.trigger_prefix and text.startswith(self.trigger_prefix):
                text = text[len(self.trigger_prefix):].lstrip(" ：:，,、").strip()
                return bool(text), text
            return False, ""

        if self.trigger == "prefix":
            prefix = self.trigger_prefix
            if not prefix:
                return True, text
            if not text.startswith(prefix):
                return False, ""
            text = text[len(prefix):].lstrip(" ：:，,、")
        return bool(text.strip()), text.strip()

    def _try_proactive(self, chat_id: str, text: str, sender_name: str = "") -> None:
        """没被 @ 时，偶尔自己接一句；先过滤掷骰，再让模型决定接不接。"""
        who = self.targets.get(chat_id) or (
            self.db.get_nickname(chat_id) if self.db else chat_id
        )
        if not self.proactive.consider(chat_id, who, text):
            return

        recent = self.proactive.recent(chat_id)
        lines = [f"{s}：{t}" for _, s, t in recent[-8:]]
        context = "\n".join(lines) if lines else f"{sender_name or '群友'}：{text}"
        prompt = (
            f"这是微信群「{who}」的聊天记录：\n{context}\n\n"
            f"最新一条是 {sender_name or '群友'}：{text}\n\n"
            "你没有被人 @。想接就接一句，不想接就沉默。"
        )
        system = self.system_prompt + "\n\n" + PROACTIVE_RULES.format(
            max_chars=self.proactive.max_chars
        )
        try:
            reply = self.api.complete(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}],
                max_tokens=120,
            )
        except LLMError as exc:
            log.debug("主动接话判断失败：%s", exc)
            return

        if self.plain_text:
            reply = to_wechat_text(reply)
        reply = (reply or "").strip().strip("“”\"'「」")
        head = reply.upper()
        if not reply or head.startswith("[SKIP]") or head in ("SKIP", "跳过"):
            log.info("主动接话：这次不接（%s）", who)
            return

        limit = self.proactive.max_chars
        if len(reply) > limit:
            cut = max(reply.rfind(ch, 0, limit) for ch in "，。！？")
            reply = reply[:cut] if cut >= limit // 2 else reply[:limit]
        reply = reply.rstrip("。，,、 ")
        if not reply:
            return

        delay = min(7.0, 0.6 + len(reply) * 0.05) + random.uniform(0.4, 1.5)
        log.info("主动接话（%s）：%.1fs 后发：%s", who, delay, reply.replace("\n", " "))
        if not self.dry_run:
            time.sleep(delay)
        if self._reply(chat_id, who, reply):
            self.proactive.note_sent(chat_id)
            self.history.append(chat_id, "user",
                                f"{sender_name}：{text}" if sender_name else text)
            self.history.append(chat_id, "assistant", reply)

    def handle(self, chat_id: str, msg: dict) -> None:
        key = (chat_id, msg.get("shard"), msg.get("local_id"))
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 20000:
            self._seen.clear()

        if self._is_self(msg):
            return
        if msg.get("type") != "文本":
            log.info("跳过非文字消息：%s（%s）", msg.get("type"), chat_id)
            return

        text = str(msg.get("content") or "").strip()
        if not text:
            return
        is_group = chat_id.endswith("@chatroom")
        raw_text = text

        sender_name = ""
        su = msg.get("sender_username") or ""
        if su and su != self.self_wxid:
            try:
                sender_name = self.db.get_nickname(su) or ""
            except Exception:
                sender_name = ""

        # 即使这条不 @ 它，也先记进群聊缓冲，主动接话时要用
        if is_group:
            self.proactive.note_message(chat_id, sender_name or "群友", raw_text)

        who = self.targets.get(chat_id) or self.db.get_nickname(chat_id)
        ok, text = self._should_reply(chat_id, text, sender_name, su)
        if not ok:
            if is_group:
                # 让日志能证明「看到了」：没 @ 的群消息也会留一行
                log.info("👀 %s / %s：%s", who, sender_name or "群友", raw_text[:80])
                if self.proactive.enabled:
                    self._try_proactive(chat_id, raw_text, sender_name)
            return

        log.info("← %s%s：%s", who, f" / {sender_name}" if is_group and sender_name else "", text)

        # 语音指令：语音说 / /voice
        voice_text = self._match_voice_command(text) if self.voice_enabled else None
        if voice_text is not None:
            if voice_text:
                log.info("语音指令：%s", voice_text[:60])
                if self.send_voice(chat_id, who, voice_text):
                    self.history.append(chat_id, "assistant", "[语音]")
            return

        # 手动扫曲库：扫歌 / 入库 / 整理曲库
        if self.sing_enabled and self._match_ingest_command(text):
            log.info("手动扫描曲库")
            self._reply(chat_id, who, "好，我看看收件箱…")
            threading.Thread(target=self._scan_inbox_once,
                             args=(chat_id, who), daemon=True).start()
            return

        # 唱歌指令：唱歌 / 唱首歌 / 唱一首 / 唱 去年夏天 / 点歌 去年夏天
        sing_hit = self._match_sing_command(text) if self.sing_enabled else None
        if sing_hit is not None:
            sing_song, sing_clip = sing_hit
            if not sing_song.strip():
                self._reply(chat_id, who, "想听哪首？说个歌名，比如「唱 晴天」。")
                return
            log.info("唱歌指令：%s（clip=%s）", sing_song, sing_clip)
            if self.netease_enabled and not self._song_in_library(sing_song):
                log.info("曲库没有《%s》，走网易云自动下载", sing_song)
                threading.Thread(
                    target=self._auto_fetch_and_sing,
                    args=(chat_id, who, sing_song, sing_clip),
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=self._sing_and_send,
                    args=(chat_id, who, sing_song, sing_clip),
                    daemon=True,
                ).start()
            return

        # 内置小命令
        if text.lower() in ("/reset", "/清空", "重置对话", "重新开始"):
            self.history.reset(chat_id)
            self._reply(chat_id, who, "好，之前的聊天我忘掉啦～")
            return

        if self.reply_delay > 0:
            self._human_pause(text)

        # 群里把说话人带上，模型才知道是谁在问
        user_content = f"{sender_name}：{text}" if is_group and sender_name else text
        self.history.append(chat_id, "user", user_content)
        system = self.system_prompt
        if self.memory_enabled and (not self.memory_chats or chat_id in self.memory_chats):
            try:
                block = self.memory.context_for(text)
                if block:
                    system = system + "\n\n" + block
            except Exception as exc:
                log.warning("读取 QQ 记忆失败：%s", exc)
        messages = [{"role": "system", "content": system}]
        messages.extend(self.history.load(chat_id))

        try:
            t_api = time.time()
            reply = self.api.complete(messages)
            if self.plain_text:
                reply = to_wechat_text(reply)
            log.info("AI 用时 %.1fs，准备发送", time.time() - t_api)
        except LLMError as exc:
            log.error("DeepSeek 调用失败：%s", exc)
            if self.error_reply:
                self._reply(chat_id, who, "我这边网络卡了一下，你稍后再发一次？")
            return

        # 回复越短回得越快，做点"在打字"的延迟
        self._human_pause(reply)

        log.info("→ %s：%s", who, reply.replace("\n", " ")[:120])
        if self._reply(chat_id, who, reply):
            self.history.append(chat_id, "assistant", reply)

    def _human_pause(self, hint: str = "") -> None:
        """按字数算一个像人在打字的停顿，避免每条都同一时刻蹦出来。"""
        base = self.reply_delay
        chars = len(hint.strip())
        if chars <= 4:
            extra = random.uniform(0.2, 0.8)
        elif chars <= 20:
            extra = random.uniform(0.8, 2.0)
        else:
            extra = random.uniform(1.5, 3.2)
        time.sleep(max(0.0, base * random.uniform(0.6, 1.4) + extra))

    def _reply(self, chat_id: str, who: str, text: str) -> bool:
        parts = split_text(text, self.split_limit)
        sent_any = False
        for i, part in enumerate(parts):
            if i:
                time.sleep(0.6)
            if self.send_text(who, part):
                sent_any = True
            else:
                log.error("发送失败，放弃剩余 %d 段", len(parts) - i - 1)
                break
        if sent_any:
            self.proactive.note_bot_reply(chat_id)
        return sent_any

    def send_text(self, who: str, text: str) -> bool:
        if self.dry_run:
            log.info("[dry-run 未发送] → %s：%s", who, text)
            return True

        with self._gui_lock:
            for attempt in (1, 2):
                try:
                    gui = self._ensure_gui()
                    resp = gui.send_msg(text, who=who, verify=self.send_verify)
                    if resp:
                        return True
                    reason = resp.get("message") if isinstance(resp, dict) else resp
                    log.warning("发送失败（第 %d 次）：%s", attempt, reason)
                    self.gui = None   # 下次重建窗口句柄
                except Exception as exc:
                    log.warning("发送异常（第 %d 次）：%s", attempt, exc)
                    self.gui = None
                time.sleep(1.5)
        return False

    # -- 语音 -------------------------------------------------------------
    def _match_voice_command(self, text: str) -> str | None:
        for prefix in ("语音说", "语音：", "/voice", "/语音"):
            if text.startswith(prefix):
                return text[len(prefix):].lstrip(" ：:，,").strip()
        return None

    def _song_in_library(self, song: str) -> bool:
        try:
            return (self.sing_songs_dir / song / "vocals.wav").is_file()
        except OSError:
            return False

    @staticmethod
    def _clean_song_name(name: str) -> str:
        s = str(name or "")
        s = re.sub(r"[\(（][^)）]*[\)）]", "", s)      # 去掉「(原唱 周杰伦)」这类后缀
        s = re.sub(r'[\\/:*?"<>|]', "_", s).strip()
        s = re.sub(r"\s+", " ", s)
        return s[:60] or f"song_{int(time.time())}"

    @staticmethod
    def _rank_candidates(found: list[dict], query: str) -> list[dict]:
        """网易云搜索结果排序：同名优先，其次包含关键词，最后才考虑别的版本。"""
        q = query.strip().lower()
        allow_version = any(k in q for k in ("live", "现场", "翻唱", "cover", "伴奏", "纯音乐", "dj", "加速", "慢速"))
        noise = ("live", "现场", "翻唱", "cover", "伴奏", "纯音乐", "dj", "0.9x", "1.1x", "remix", "片段", "原唱", "女声", "男声", "抖音")

        def score(s: dict) -> int:
            text = f"{s.get('name', '')} {s.get('alias', '')}".lower()
            sc = 0
            if s.get("name", "").strip().lower() == q:
                sc -= 100
            if q and q in text:
                sc -= 20
            if not allow_version and any(k in text for k in noise):
                sc += 100
            if s.get("vip"):
                sc += 5
            dur = int(s.get("durationSec") or 0)
            if dur < 60 or dur > 600:
                sc += 50
            return sc

        return sorted(found, key=score)

    def _auto_fetch_and_sing(self, chat_id: str, who: str, song: str, clip: float) -> None:
        """库里没有 → 网易云搜歌、下载、Demucs 分离、入库，然后唱。"""
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            log.info("网易云搜索：%s", song)
            found = netease.search_songs(song, self.netease_cookie, self.netease_limit)
            if not found:
                self._reply(chat_id, who, f"网易云没搜到《{song}》，换个说法试试？")
                return

            ordered = self._rank_candidates(found, song)
            q_tokens = [t for t in re.split(r"\s+", song) if t]
            matched = [c for c in ordered
                       if any(t.lower() in self._clean_song_name(c.get("name", "")).lower() for t in q_tokens)]
            if matched:
                ordered = matched

            for cand in ordered:
                title = self._clean_song_name(cand.get("name", ""))
                if self._song_in_library(title):
                    self._sing_and_send(chat_id, who, title, clip)
                    return

                try:
                    u = netease.song_url(cand.get("id"), self.netease_cookie)
                except Exception as exc:
                    log.warning("取歌地址失败 %s：%s", cand.get("id"), exc)
                    continue
                if not u.get("url") or u.get("trial"):
                    continue

                dest = self.sing_inbox / "_auto" / f"{title}.mp3"
                try:
                    netease.download(u["url"], dest)
                except Exception as exc:
                    log.warning("下载失败 %s：%s", cand.get("id"), exc)
                    continue

                result = self._ingest_song(dest)
                if result == "fail":
                    log.warning("分离失败，换一版重试：%s", title)
                    continue

                self._sing_and_send(chat_id, who, title, clip)
                return

            self._reply(chat_id, who,
                        f"《{song}》搜到的版本都要会员或有版权限制，拿不到完整音频。"
                        "配一下网易云 Cookie 再试。")
        except Exception as exc:
            log.exception("网易云自动下歌失败")
            self._reply(chat_id, who, f"自动找歌失败了：{str(exc)[:80]}")
        finally:
            self._fetch_lock.release()

    def _match_sing_command(self, text: str) -> tuple[str, float] | None:
        t = text.strip()
        for prefix in ("唱首歌", "唱首", "唱一首", "唱一支", "唱一个", "唱个",
                       "唱歌", "来一首歌", "来一首", "来首",
                       "点一首", "点歌", "唱 "):
            if t == prefix:
                return self.sing_default, 0.0
            if t.startswith(prefix):
                rest = t[len(prefix):].lstrip(" ：:，,《》").rstrip("》").strip()
                clip = 0.0
                for kw in ("整首", "完整", "全曲"):
                    if kw in rest:
                        clip = -1.0
                        rest = rest.replace(kw, "").strip()
                        break
                else:
                    for kw in ("副歌部分", "副歌", "高潮部分", "短版", "30秒", "三十秒"):
                        if kw in rest:
                            clip = 30.0
                            rest = rest.replace(kw, "", 1).strip()
                            break
                    rest = re.sub(r"(部分|片段|那一段|一段)$", "", rest).strip()
                    rest = rest.rstrip(" 的").strip()
                return (rest or self.sing_default), clip
        return None

    # -- 曲库自动入库 -----------------------------------------------------
    @staticmethod
    def _tool_env() -> dict:
        """Seed-VC / Demucs 子进程用的环境（缓存写到 tools/cache）。"""
        env = os.environ.copy()
        env.update({
            "USERPROFILE": str(TOOLS_DIR / "cache" / "indexhome"),
            "HOME": str(TOOLS_DIR / "cache" / "indexhome"),
            "HF_HOME": str(TOOLS_DIR / "cache" / "indexhome" / "hf"),
            "HF_ENDPOINT": "https://hf-mirror.com",
            "MPLCONFIGDIR": str(TOOLS_DIR / "cache" / "indexhome" / "matplotlib"),
            "PYTHONUTF8": "1",
        })
        return env

    def _notify(self, text: str) -> None:
        """给「文件传输助手」发一条进度，不打扰聊天对象。"""
        try:
            self._reply("filehelper", "文件传输助手", text)
        except Exception:
            log.debug("入库通知发送失败：\n%s", traceback.format_exc())

    def _move_inbox(self, audio: Path, sub: str) -> None:
        try:
            dest = self.sing_inbox / sub
            dest.mkdir(parents=True, exist_ok=True)
            audio.replace(dest / audio.name)
        except OSError:
            log.warning("收件箱文件移动失败：%s", audio)

    def _ingest_song(self, audio: Path) -> str:
        """一个音频文件 → Demucs 分离 → songs/<歌名>/{vocals,instrumental}.wav

        返回 "ok" / "skip" / "fail"，给手动扫描统计用。
        """
        name = re.sub(r'[\\/:*?"<>|]', "_", audio.stem).strip()[:60]
        if not name:
            name = f"song_{int(time.time())}"
        target = self.sing_songs_dir / name
        if (target / "vocals.wav").is_file():
            log.info("曲库已有《%s》，跳过", name)
            self._move_inbox(audio, "_done")
            return "skip"

        seed = (TOOLS_DIR / "ai" / "seed-vc")
        py = seed / ".venv" / "Scripts" / "python.exe"
        if not py.is_file():
            log.error("Demucs 环境缺失：%s", py)
            self._move_inbox(audio, "_failed")
            return "fail"

        work = DATA_DIR / "ingest" / f"{int(time.time())}_{name}"
        work.mkdir(parents=True, exist_ok=True)
        log.info("曲库入库开始：《%s》← %s", name, audio.name)
        try:
            proc = subprocess.run(
                [str(py), "-m", "demucs", "--two-stems=vocals",
                 "-n", "htdemucs", "-o", str(work), str(audio)],
                cwd=str(seed), env=self._tool_env(),
                capture_output=True, text=True, timeout=1800,
            )
            vocals = next(iter(work.glob("htdemucs/*/vocals.wav")), None)
            inst = next(iter(work.glob("htdemucs/*/no_vocals.wav")), None)
            if proc.returncode != 0 or vocals is None or inst is None:
                raise RuntimeError((proc.stderr or "")[-400:] or "Demucs 没有输出")
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vocals, target / "vocals.wav")
            shutil.copy2(inst, target / "instrumental.wav")
            self._move_inbox(audio, "_done")
            log.info("曲库入库完成：《%s》", name)
            return "ok"
        except Exception as exc:
            log.error("曲库入库失败 %s：%s", name, exc)
            self._move_inbox(audio, "_failed")
            return "fail"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _match_ingest_command(text: str) -> bool:
        return text.strip().lower() in (
            "扫歌", "扫一下歌", "扫曲库", "扫描曲库", "扫一下曲库",
            "入库", "整理曲库", "更新曲库", "曲库入库",
        )

    def _scan_inbox_once(self, chat_id: str, who: str) -> None:
        """手动触发：把收件箱里的音频全部入库一次。"""
        exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
        try:
            files = sorted(p for p in self.sing_inbox.iterdir()
                           if p.is_file() and p.suffix.lower() in exts)
        except OSError as exc:
            self._reply(chat_id, who, f"收件箱打不开：{exc}")
            return
        now = time.time()
        ready = [p for p in files if now - p.stat().st_mtime > 3]
        if not ready:
            self._reply(chat_id, who, f"收件箱里没有待处理的歌（{self.sing_inbox}）")
            return

        self._reply(chat_id, who, f"扫到 {len(ready)} 个文件，开始入库…")
        ok = skip = fail = 0
        for p in ready:
            r = self._ingest_song(p)
            if r == "ok":
                ok += 1
            elif r == "skip":
                skip += 1
            else:
                fail += 1
        parts = [f"入库 {ok} 首"]
        if skip:
            parts.append(f"已存在 {skip} 首")
        if fail:
            parts.append(f"失败 {fail} 首")
        self._reply(chat_id, who, "扫完了：" + "，".join(parts))

    def _watch_music_inbox(self) -> None:
        """盯着收件箱，文件大小稳定两轮才处理，避免拷到一半。"""
        exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
        try:
            self.sing_inbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("曲库收件箱不可用：%s", exc)
            return
        log.info("曲库收件箱：%s（每 %d 秒扫一次）",
                 self.sing_inbox, self.sing_ingest_interval)
        stable: dict[str, int] = {}
        while not self._stop.wait(self.sing_ingest_interval):
            try:
                files = sorted(p for p in self.sing_inbox.iterdir()
                               if p.is_file() and p.suffix.lower() in exts)
            except OSError:
                continue
            for p in files:
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size <= 0:
                    continue
                if stable.get(p.name) != size:
                    stable[p.name] = size
                    continue
                stable.pop(p.name, None)
                self._ingest_song(p)

    @staticmethod
    def _cable_input_device() -> int | None:
        """找 CABLE Input 的 WASAPI 2ch 48k 播放设备。"""
        import sounddevice as sd

        devices = sd.query_devices()
        for want_name in ("CABLE Input", "CABLE In"):
            for i, d in enumerate(devices):
                if want_name in d["name"] and d["max_output_channels"] >= 2 \
                        and int(d["default_samplerate"]) == 48000:
                    return i
        for i, d in enumerate(devices):
            if "CABLE" in d["name"] and d["max_output_channels"] >= 2:
                return i
        return None

    def _tts_to_wav(self, text: str, wav_path: Path) -> bool:
        import edge_tts
        import imageio_ffmpeg

        mp3 = wav_path.with_suffix(".mp3")
        kwargs = {}
        if self.voice_rate:
            kwargs["rate"] = self.voice_rate
        if self.voice_pitch:
            kwargs["pitch"] = self.voice_pitch
        asyncio.run(edge_tts.Communicate(text, self.voice_name, **kwargs).save(str(mp3)))
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg, "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2", str(wav_path)],
            check=True, capture_output=True,
        )
        try:
            mp3.unlink()
        except OSError:
            pass
        return wav_path.exists()

    @staticmethod
    def _audio_duration(path: Path, ff: str) -> float:
        """用 ffmpeg -i 的 stderr 读时长，读不到返回 0。"""
        try:
            proc = subprocess.run(
                [ff, "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return 0.0

    def _auto_clip_vocals(self, vocals: Path, outdir: Path, seconds: float, ff: str) -> Path:
        """整首太长时，自动截一段：auto 从 30% 处开始，head 从头。"""
        dur = self._audio_duration(vocals, ff)
        if dur <= seconds + 1:
            return vocals
        start = 0.0 if self.sing_clip_start == "head" else min(
            max(0.0, dur * 0.3), max(0.0, dur - seconds)
        )
        out = outdir / "vocals_auto_clip.wav"
        fade = f"afade=t=in:d=1.0,afade=t=out:st={max(0.0, seconds - 2):.1f}:d=2"
        try:
            subprocess.run(
                [ff, "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.1f}", "-i", str(vocals),
                 "-af", fade, "-ar", "44100", "-ac", "1", str(out)],
                capture_output=True, timeout=300,
            )
        except Exception as exc:
            log.warning("自动截取失败：%s", exc)
            return vocals
        if out.is_file() and out.stat().st_size > 4096:
            log.info("自动截取 %.0f 秒（从 %.1f 秒开始）", seconds, start)
            return out
        return vocals

    def _sing_and_send(self, chat_id: str, who: str, song: str,
                       clip_seconds: float = 0.0) -> None:
        """Demucs（可选）→ Seed-VC 换音色 → 混音 → 发语音条。"""
        if not self._sing_lock.acquire(blocking=False):
            self._reply(chat_id, who, "上一首还在唱，等我唱完这口气…")
            return
        try:
            song_dir = self.sing_songs_dir / song
            vocals = song_dir / "vocals.wav"
            inst = song_dir / "instrumental.wav"
            # 清唱模式不需要伴奏，只要人声在就能唱
            need_inst = self.sing_with_music
            if not (song_dir.is_dir() and vocals.is_file()
                    and (inst.is_file() or not need_inst)):
                names = []
                if self.sing_songs_dir.is_dir():
                    names = [p.name for p in self.sing_songs_dir.iterdir() if p.is_dir()]
                tip = "、".join(names) if names else "（空）"
                self._reply(chat_id, who, f"曲库里还没《{song}》。现有：{tip}")
                return

            if not self.sing_ref or not Path(self.sing_ref).is_file():
                self._reply(chat_id, who, "还没设置唱歌用的参考音，先让我主人配一下。")
                return

            seed = (TOOLS_DIR / "ai" / "seed-vc")
            py = seed / ".venv" / "Scripts" / "python.exe"
            if not py.is_file():
                self._reply(chat_id, who, "唱歌引擎还没装好，先让我主人看一下。")
                return

            outdir = DATA_DIR / "sing" / f"{int(time.time())}"
            outdir.mkdir(parents=True, exist_ok=True)
            env = self._tool_env()

            source_vocals = vocals
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            if clip_seconds is not None and clip_seconds < 0:
                log.info("按「整首」处理，不截取")
            elif clip_seconds and clip_seconds > 0:
                # 明确要一小段（副歌）：先掐掉开头静音，再截到指定长度
                clip_path = outdir / "vocals_clip.wav"
                for af in ("silenceremove=start_periods=1:start_silence=0.2:start_threshold=-45dB",
                           "anull"):
                    subprocess.run(
                        [ff, "-y", "-i", str(vocals), "-af", af,
                         "-t", str(clip_seconds), "-ar", "44100", "-ac", "1",
                         str(clip_path)],
                        capture_output=True, timeout=180,
                    )
                    if clip_path.is_file() and clip_path.stat().st_size > 4096:
                        source_vocals = clip_path
                        break
                if source_vocals is vocals:
                    log.warning("截取副歌失败，改用整首")
            elif self.sing_max_seconds > 0:
                source_vocals = self._auto_clip_vocals(vocals, outdir, self.sing_max_seconds, ff)

            cli = (BASE_DIR / "sing_convert.py")
            cmd = [
                sys.executable, str(cli),
                "--input", str(source_vocals),
                "--output-dir", str(outdir),
                "--ref", str(self.sing_ref),
                "--steps", str(self.sing_steps),
            ]
            log.info("唱歌：换声开始（%s）", song)
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900)
            if proc.returncode != 0:
                log.error("换声失败：%s", ((proc.stderr or "") + (proc.stdout or ""))[-800:])
                self._reply(chat_id, who, "这次没唱成，转换引擎报错了…")
                return
            vc = next(iter(outdir.glob("vc_*.wav")), None)
            if vc is None:
                self._reply(chat_id, who, "这次没唱成，没生成出音频…")
                return

            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            if self.sing_with_music:
                mixed = outdir / "mix.mp3"
                subprocess.run([
                    ff, "-y", "-i", str(vc), "-i", str(inst),
                    "-filter_complex",
                    "[0:a]volume=-2dB,highpass=f=70[v];[1:a]volume=-3dB[m];"
                    "[v][m]amix=inputs=2:duration=first:dropout_transition=0,"
                    "alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-b:a", "192k", str(mixed),
                ], check=True, timeout=300, capture_output=True)
                audio_src = mixed
                audio_filter = None
            else:
                audio_src = vc
                audio_filter = "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11"

            full = outdir / "full.wav"
            full_cmd = [ff, "-y", "-i", str(audio_src)]
            if audio_filter:
                full_cmd += ["-af", audio_filter]
            full_cmd += ["-ar", "48000", "-ac", "1", "-sample_fmt", "s16", str(full)]
            subprocess.run(full_cmd, check=True, timeout=120, capture_output=True)

            import wave

            import numpy as np
            with wave.open(str(full), "rb") as w:
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            max_len = int(self.sing_max_segment * sr)
            segments = [pcm[i:i + max_len] for i in range(0, len(pcm), max_len)]
            if self.sing_send_mode == "voice":
                segments = segments[:1]
            for i, seg in enumerate(segments):
                seg_path = outdir / f"seg{i}.wav"
                with wave.open(str(seg_path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(seg.tobytes())
                ok = self._send_voice_wav(chat_id, who, seg_path)
                log.info("唱歌：第 %d/%d 段 %s", i + 1, len(segments), "ok" if ok else "fail")
                time.sleep(0.8)
            self._reply(chat_id, who, f"唱完啦～《{song}》分 {len(segments)} 条发你了")
        except Exception:
            log.error("唱歌失败：\n%s", traceback.format_exc())
            try:
                self._reply(chat_id, who, "这次没唱成，稍后再试一次？")
            except Exception:
                pass
        finally:
            self._sing_lock.release()

    def _send_voice_wav(self, chat_id: str, who: str, wav_path: Path) -> bool:
        """把现成 WAV 播放到 CABLE Input，按住右 Alt 发成微信语音条。"""
        import numpy as np
        import sounddevice as sd
        import wave
        import win32api
        import win32con
        import win32gui
        from wechatauto.uia_driver import WeChatUIA

        with wave.open(str(wav_path), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            pcm = raw.reshape(-1, ch).mean(axis=1) if ch > 1 else raw
        if sr != 48000:
            idx = np.linspace(0, len(pcm) - 1, int(len(pcm) * 48000 / sr))
            pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float32)
        stereo = np.column_stack([pcm, pcm])
        dev = self._cable_input_device()
        if dev is None:
            log.error("找不到 CABLE Input 播放设备")
            return False

        with self._gui_lock:
            uia = WeChatUIA()
            uia.ensure_window()
            uia.open_chat(who)
            time.sleep(0.6)
            box = uia._chat_input()
            if box is not None:
                box.SetFocus()
                try:
                    win32gui.SetForegroundWindow(win32gui.FindWindow(None, "微信"))
                except Exception:
                    pass
                time.sleep(0.5)
                rect = box.BoundingRectangle
                win32api.SetCursorPos(((rect.left + rect.right) // 2,
                                       (rect.top + rect.bottom) // 2))
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                box.SetFocus()
            time.sleep(0.6)
            stream = sd.OutputStream(
                device=dev, samplerate=48000, channels=2, dtype="float32",
                extra_settings=sd.WasapiSettings(exclusive=False),
            )
            stream.start()
            win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY, 0)
            time.sleep(0.5)
            step = 4800
            for i in range(0, len(stereo), step):
                stream.write(stereo[i:i + step])
            time.sleep(0.5)
            win32api.keybd_event(
                0xA5, 0x38,
                win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0,
            )
            stream.stop()
            stream.close()
        return True

    def send_voice(self, chat_id: str, who: str, text: str) -> bool:
        """TTS → CABLE Input → 按住右 Alt 让微信录音 → 松开成语音条。"""
        text = (text or "").strip()[: self.voice_max_chars]
        if not text:
            return False
        if self.dry_run:
            log.info("[dry-run 语音] → %s：%s", who, text)
            return True

        wav = DATA_DIR / "voice" / f"tts_{int(time.time() * 1000)}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not self._tts_to_wav(text, wav):
                log.error("TTS 生成失败")
                return False

            import wave

            import numpy as np
            import sounddevice as sd
            import win32api
            import win32con
            import win32gui
            from wechatauto.uia_driver import WeChatUIA

            with wave.open(str(wav), "rb") as w:
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()),
                                     dtype=np.int16).astype(np.float32) / 32768.0
            if sr != 48000:
                idx = np.linspace(0, len(pcm) - 1, int(len(pcm) * 48000 / sr))
                pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float32)
            stereo = np.column_stack([pcm, pcm])

            dev = self._cable_input_device()
            if dev is None:
                log.error("找不到 CABLE Input 播放设备")
                return False

            with self._gui_lock:
                uia = WeChatUIA()
                uia.ensure_window()
                uia.open_chat(who)
                time.sleep(0.6)
                box = uia._chat_input()
                if box is not None:
                    box.SetFocus()
                    try:
                        win32gui.SetForegroundWindow(win32gui.FindWindow(None, "微信"))
                    except Exception:
                        pass
                    time.sleep(0.5)
                    rect = box.BoundingRectangle
                    win32api.SetCursorPos(((rect.left + rect.right) // 2,
                                           (rect.top + rect.bottom) // 2))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    box.SetFocus()
                time.sleep(0.6)

                stream = sd.OutputStream(
                    device=dev, samplerate=48000, channels=2, dtype="float32",
                    extra_settings=sd.WasapiSettings(exclusive=False),
                )
                stream.start()
                win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY, 0)
                time.sleep(0.5)
                step = 4800
                for i in range(0, len(stereo), step):
                    stream.write(stereo[i:i + step])
                time.sleep(0.5)
                win32api.keybd_event(
                    0xA5, 0x38,
                    win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0,
                )
                stream.stop()
                stream.close()

            log.info("语音已发送（%.1fs）→ %s", len(pcm) / 48000, who)
            time.sleep(2.0)
            try:
                recent = self.db.get_messages(chat_id, limit=3)
                if any(m.get("type") == "语音" for m in recent):
                    log.info("语音条已落库 ✓")
            except Exception as exc:
                log.debug("语音回读失败：%s", exc)
            return True
        except Exception:
            log.error("语音发送失败：\n%s", traceback.format_exc())
            return False
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    # -- 主循环 -----------------------------------------------------------
    def _watch_listener(self, listener) -> None:
        """监听线程的健康巡检：轮询异常只写 stderr 的话，日志里会看不见。"""
        while not self._stop.wait(30):
            try:
                thread = getattr(listener, "_thread", None)
                if thread is not None and not thread.is_alive():
                    log.error("监听线程已退出，正在重启")
                    listener.start()
            except Exception:
                log.debug("监听巡检异常：\n%s", traceback.format_exc())

    def start(self) -> None:
        from wechatauto.db import Listener

        self.listener = Listener(self.db, interval=1.0)
        if self.listen_all:
            self.listener.add_all(lambda msg, lst: self._safe_handle(msg), discover=True)
        else:
            for chat_id, who in self.targets.items():
                self.listener.add_listener(chat_id, self._make_callback(chat_id))
        self.listener.start()
        threading.Thread(target=self._watch_listener, args=(self.listener,),
                         name="listener-watch", daemon=True).start()
        if self.sing_enabled and self.sing_auto_ingest:
            threading.Thread(target=self._watch_music_inbox,
                             name="music-inbox", daemon=True).start()

    def _safe_handle(self, msg: dict) -> None:
        try:
            chat_id = msg.get("username") or ""
            if not chat_id:
                return
            self.handle(chat_id, msg)
        except Exception:
            log.error("处理消息出错：\n%s", traceback.format_exc())

    def run(self) -> None:
        self.validate_config()
        self.connect()
        self.targets = {} if self.listen_all else self.resolve_targets()

        print("\n" + "=" * 58)
        print(f"  DeepSeek × 微信   |   机器人：{self.name}")
        print("=" * 58)
        print(f"  模型：{self.api.model}")
        if self.listen_all:
            print("  监听：全部聊天")
        else:
            for chat_id, who in self.targets.items():
                print(f"  监听：{who}（{chat_id}）")
        print(f"  触发：{self.trigger}" + (f"（前缀 {self.trigger_prefix}）" if self.trigger == "prefix" else ""))
        if self.proactive.enabled:
            print(f"  主动接话：{', '.join(sorted(self.proactive.groups)) or '未配置群'}"
                  f"（{self.proactive.mode}，{self.proactive.active_hours} 点）")
        if self.voice_enabled:
            print(f"  语音条：{self.voice_name}（说「语音说 …」）")
        print(f"  记忆：{self.history.max_messages} 条")
        print("-" * 58)
        print("  保持微信在线，别锁屏；按 Ctrl+C 停止")
        print("=" * 58 + "\n")

        self.warmup_gui()
        self.start()
        log.info("开始监听新消息…")

        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            print()
            log.info("收到 Ctrl+C，正在停止…")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
        log.info("已停止。")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def cmd_check(cfg: Config) -> int:
    print("\n=== 环境自检 ===\n")
    ok_all = True

    # 1. 配置
    bot = WeChatBot(cfg, dry_run=True)
    try:
        bot.validate_config()
        print("✓ 配置完整（api_key / base_url / 监听名单）")
    except ConfigError as exc:
        print(f"✗ 配置有问题：{exc}")
        return 1

    # 2. API
    print(f"· 测试 DeepSeek 接口：{bot.api.base_url}  模型 {bot.api.model}")
    try:
        reply = bot.api.complete(
            [{"role": "user", "content": "只回复两个字：在的"}], max_tokens=32
        )
        print(f"✓ API 连通，模型回复：{reply[:40]}")
    except LLMError as exc:
        print(f"✗ API 不通：{exc}")
        ok_all = False

    # 3. 微信数据库
    try:
        bot.connect()
        print(f"✓ 微信数据库可读，账号：{bot.self_name or bot.self_wxid}")
    except BotError as exc:
        print(f"✗ {exc}")
        return 1

    # 4. 监听对象
    try:
        targets = bot.resolve_targets()
        if targets:
            print("✓ 监听对象：")
            for chat_id, who in targets.items():
                print(f"    {who}  ({chat_id})")
        else:
            print("! 没有监听对象（listen_all = true？）")
    except BotError as exc:
        print(f"✗ {exc}")
        ok_all = False

    print("\n" + ("全部通过，可以运行「启动.bat」了。" if ok_all else "有项目没通过，按上面提示处理。"))
    return 0 if ok_all else 1


def cmd_sessions() -> int:
    from wechatauto import WeChatDB

    try:
        db = WeChatDB(workdir=str(CACHE_DIR))
    except RuntimeError as exc:
        print(f"读取微信数据库失败：{exc}\n请先登录微信。")
        return 1
    info = db.get_self_info() or {}
    print(f"\n当前账号：{info.get('nick_name') or info.get('username')}\n")
    print(f"{'类型':<4} {'显示名':<20} {'username':<32} 未读")
    print("-" * 74)
    for s in db.get_sessions(limit=60):
        name = db.get_nickname(s["username"]) or s["username"]
        tag = "群" if s["username"].endswith("@chatroom") else "友"
        print(f"[{tag}]  {name[:18]:<20} {s['username']:<32} {s['unread']}")
    return 0


def cmd_ask(cfg: Config, text: str) -> int:
    bot = WeChatBot(cfg, dry_run=True)
    try:
        bot.validate_config()
    except ConfigError as exc:
        print(f"配置有问题：\n{exc}")
        return 1
    messages = [
        {"role": "system", "content": bot.system_prompt},
        {"role": "user", "content": text},
    ]
    try:
        reply = bot.api.complete(messages)
    except LLMError as exc:
        print(f"调用失败：{exc}")
        return 1
    print(reply)
    return 0


def _disable_console_quickedit() -> None:
    """关掉控制台的「快速编辑」模式。

    Windows 控制台一旦被鼠标选中文字就会进入选择模式，此后任何 stdout 写入都会
    阻塞 —— 机器人日志写 stdout，于是整个进程（包括监听线程）卡死。
    这就是「发消息没反应」反复出现的真正原因之一。
    """
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        ENABLE_QUICK_EDIT = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS
        k32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek × 微信 自动回复机器人")
    parser.add_argument("--check", action="store_true", help="环境自检")
    parser.add_argument("--sessions", action="store_true", help="列出微信会话")
    parser.add_argument("--ask", metavar="TEXT", help="只测 DeepSeek，不碰微信")
    parser.add_argument("--dry-run", action="store_true", help="监听但不真的发消息")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    _disable_console_quickedit()
    # 原生崩溃（UIA/COM/SQLite 段错误）不会留 Python traceback，
    # 用 faulthandler 把崩溃现场的线程栈写到 logs\crash.log
    global _CRASH_FILE
    try:
        import faulthandler
        _CRASH_FILE = open(LOG_DIR / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_CRASH_FILE, all_threads=True)
    except Exception:
        pass
    setup_logging(args.verbose)
    quiet_third_party()

    try:
        cfg = Config()
    except ConfigError as exc:
        print(exc)
        return 1

    if args.sessions:
        return cmd_sessions()
    if args.check:
        return cmd_check(cfg)
    if args.ask:
        return cmd_ask(cfg, args.ask)

    try:
        lock = acquire_single_instance()
        if lock is None:
            print(
                "\n已经有一个机器人在运行了。\n"
                "如果没看到它的窗口，打开任务管理器结束所有 python.exe 再试。\n"
            )
            return 1
        WeChatBot(cfg, dry_run=args.dry_run).run()
    except ConfigError as exc:
        print(f"\n配置有问题：\n{exc}\n")
        return 1
    except BotError as exc:
        print(f"\n启动失败：\n{exc}\n")
        return 1
    except Exception:
        log.error("程序异常退出：\n%s", traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
