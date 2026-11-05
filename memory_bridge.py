"""只读读取 Aurabot（QQ 插件）的记忆，给微信机器人用。

数据源：
    <Aurabot 数据目录>/memories.json（见 config.example.ini [memory] file）

原则：
  - 只读。不写、不锁、不修改 QQ 插件任何文件。
  - 按 mtime 缓存，插件更新记忆后微信这边自动看到。
  - 只在配置允许的微信会话里注入（默认只私聊主人），避免把私人记忆带到群里。

自测：
    python memory_bridge.py 今天好累
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

STOP = set("的了吧呢吗啊嗯哦呀哈是在我你他她它这那有和就不都也还又很太把被让给对从到与或而但")

STOP_WORDS = {
    "今天", "昨天", "明天", "现在", "时候", "一个", "什么", "怎么", "可以",
    "我们", "他们", "自己", "这个", "那个", "还是", "就是", "没有", "觉得",
    "知道", "真的", "有点", "一点", "已经", "然后", "因为", "所以", "但是",
    "如果", "不是", "这么", "那么", "一下", "一样", "东西", "事情",
}

WORD_RE = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in WORD_RE.finditer(str(text or "").lower()):
        w = m.group(0)
        if re.fullmatch(r"[a-z0-9_]+", w):
            out.append(w)
            continue
        if len(w) == 1:
            if w not in STOP:
                out.append(w)
            continue
        for i in range(len(w) - 1):
            pair = w[i:i + 2]
            if pair in STOP_WORDS:
                continue
            if not (pair[0] in STOP and pair[1] in STOP):
                out.append(pair)
    return out


def _days_ago(ms: int) -> float:
    try:
        return max(0.0, (datetime.now().timestamp() * 1000 - int(ms)) / 86400000)
    except Exception:
        return 999.0


class MemoryStore:
    def __init__(self, path: str, owner_qq: str = "", top_k: int = 6):
        self.path = Path(path)
        self.owner_qq = str(owner_qq or "")
        self.top_k = max(1, int(top_k))
        self._mtime = -1.0
        self._data: dict = {}
        self._docs: list[dict] = []
        self._df: dict[str, int] = {}
        self._avg_len = 1.0
        self._N = 0

    # ---------- 加载 ----------
    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        if mtime == self._mtime:
            return self._data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._data or {}

        docs = []
        df: dict[str, int] = {}
        total = 0
        for it in data.get("items") or []:
            if it.get("invalidAt"):
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            tokens = tokenize(" ".join([text, it.get("aboutName") or "", " ".join(it.get("tags") or [])]))
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
            docs.append({"it": it, "tf": tf, "len": max(1, len(tokens))})
            total += max(1, len(tokens))

        self._data = data
        self._docs = docs
        self._df = df
        self._N = max(1, len(docs))
        self._avg_len = total / self._N if docs else 1.0
        self._mtime = mtime
        return data

    # ---------- 检索 ----------
    def recall(self, query: str, limit: int = 4) -> list[dict]:
        self._load()
        q = [t for t in dict.fromkeys(tokenize(query)) if t not in STOP]
        if not q or not self._docs:
            return []
        k1, b = 1.4, 0.7
        scored = []
        for d in self._docs:
            s = 0.0
            for t in q:
                f = d["tf"].get(t, 0)
                if not f:
                    continue
                n = self._df.get(t, 0)
                idf = math.log(1 + (self._N - n + 0.5) / (n + 0.5))
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d["len"] / self._avg_len))
            if s <= 0:
                continue
            it = d["it"]
            s += 1.2 * float(it.get("importance") or 0.5)
            s += 0.15 * math.log1p(float(it.get("hits") or 0))
            s += 0.5 * math.exp(-_days_ago(it.get("at") or 0) / 120)
            scored.append((s, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:limit]]

    def profile_text(self) -> str:
        data = self._load()
        p = (data.get("profiles") or {}).get(self.owner_qq)
        if isinstance(p, dict):
            return str(p.get("text") or "").strip()
        return ""

    def insights(self) -> list[str]:
        data = self._load()
        arr = (data.get("insights") or {}).get(f"qq:{self.owner_qq}") or []
        return [str(x.get("text") or "").strip() for x in arr if isinstance(x, dict) and x.get("text")]

    # ---------- 拼提示词 ----------
    def context_for(self, query: str, limit: int | None = None) -> str:
        if not self.path.is_file():
            return ""
        lines: list[str] = []
        prof = self.profile_text()
        if prof:
            lines.append("你对主人的印象：" + prof)
        ins = self.insights()
        if ins:
            lines.append("和主人相处的经验：" + "；".join(ins[:6]))

        hits = self.recall(query, limit or self.top_k)
        seen = {prof}
        for it in hits:
            text = str(it.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ts = it.get("at")
            when = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d") if ts else ""
            lines.append(f"[{when}] {text}" if when else text)

        if not lines:
            return ""
        body = "\n".join("- " + ln for ln in lines)
        if len(body) > 900:
            body = body[:900] + "…"
        return (
            "（下面是你记得的事，聊到相关的就自然用上；别把老事当新事说，"
            "也不要说「根据记忆」：\n" + body + "\n）"
        )


def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    default = os.environ.get("AURABOT_MEMORY", str(Path(__file__).parent / "data" / "qq_memories.json"))
    store = MemoryStore(default, os.environ.get("AURABOT_OWNER_QQ", ""), 6)
    q = " ".join(argv) or "你好"
    print(store.context_for(q))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
