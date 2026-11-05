# 架构

```text
微信
  ↓ 数据库（wechatauto-replica）
Listener → 消息去重 → 会话判断（私聊/群/@）
  ↓
命令层：唱歌 / 扫歌 / 重置 / 普通聊天
  ↓
DeepSeek（OpenAI 兼容接口）
  ↓
分条 → 文字 或 edge-tts 语音
  ↓
微信 UIA 发送（语音走 VB-Cable + 右 Alt）
```

## 唱歌链路

```text
唱 歌名
  ↓ 曲库有？
  ↓ 没有 → 网易云搜索 → 下载 → Demucs 分离 → 入库
vocals.wav
  ↓ 按 max_seconds 截一段（默认 60 秒，从 30% 处开始）
sing_convert.py
  ↓ Seed-VC：--f0-condition True --fp16
vc.wav
  ↓ 切 19 秒一段
语音条
```

分离和换声是两件事：

| 步骤 | 模型 |
| --- | --- |
| 人声分离 | Demucs |
| 换声 | Seed-VC |
| 说话 | edge-tts（可选 IndexTTS） |

## 网易云

`netease.py` 实现网页版 weapi：

- AES-128-CBC 两次 + RSA 无填充
- `cloudsearch/pc` 搜歌
- `song/enhance/player/url/v1` 取地址
- 用 `freeTrialInfo` 识别试听
- 候选按歌名过滤，拿不到完整音频就换下一个

## 记忆桥（可选）

只读 `memories.json`：

```text
items / profiles / insights
  ↓ 中文二元切词 + BM25 + importance + 时间衰减
  ↓ 拼进 system prompt
只在配置的私聊里生效
```
