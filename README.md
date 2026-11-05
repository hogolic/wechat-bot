# DeepSeek × 微信机器人

把 DeepSeek 接进微信：读本机微信数据库拿消息，用 DeepSeek 生成回复，再驱动微信窗口发出去。

> ⚠️ **非官方接入，仅供个人学习。** 微信对自动化有风控，可能封号；请先用小号或「文件传输助手」测试，不要直接挂主号大群。
>
> 语音克隆、翻唱、下载歌曲还涉及**声音授权**和**版权**，请确认你有权使用。

## 功能

- **聊天**：私聊 / 群聊 @ / 关键词触发，DeepSeek 回复
- **人设**：改 `personas/*.txt` 就能换人格
- **语音条**：edge-tts 合成，走 VB-Cable 录进微信
- **主动接话**：群里冷场时按概率插一句（可选）
- **唱歌**：Seed-VC 把源人声换成参考音色，切段发语音条
- **自动搜歌**：曲库没有时去网易云搜、下载、Demucs 分离、入库、再唱
- **手动入库**：把 mp3 丢进 `music_inbox`，发「扫歌」
- **共享记忆**（可选）：只读 Aurabot（QQ 插件）的 `memories.json`

## 环境要求

- Windows 10/11
- Python 3.12
- 微信 PC 4.1.12+（本项目在 4.1.15 上测试）
- 微信以**管理员身份**运行，机器人也要管理员权限
- 发语音条还需要：
  - [VB-Audio Cable](https://vb-audio.com/Cable/)（虚拟声卡）
  - 系统默认麦克风设为 `CABLE Output`
  - `ffmpeg`（唱歌、切音频用）

## 安装

```bat
install.bat
```

或手动：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

然后：

```text
copy config.example.ini config.ini
```

填 `[deepseek] api_key`，再按需改监听名单。

启动：

```bat
start.bat
```

首次启动要解密微信数据库，约 10~20 秒。

## 配置

| 段 | 说明 |
| --- | --- |
| `[deepseek]` | OpenAI 兼容接口、模型、温度 |
| `[wechat]` | 监听谁、群聊开关、@ 名字 |
| `[proactive]` | 主动接话（默认关） |
| `[voice]` | edge-tts 语音 |
| `[sing]` | 唱歌、曲库、参考音、截取长度 |
| `[netease]` | 自动搜歌（cookie 可选） |
| `[memory]` | 只读共用 QQ 插件记忆（默认关） |
| `[persona]` | 人设文件 |

路径都支持相对路径，相对于仓库根目录。

## 唱歌怎么配

### 1. 参考音

```text
tools/data/refs/ref.wav
```

10~30 秒、单人、干净、无伴奏。**必须本人授权。**

### 2. 曲库

```text
tools/data/songs/<歌名>/
  vocals.wav          人声
  instrumental.wav    伴奏
```

两种入库方式：

```text
手动：把 mp3 放进 tools/data/music_inbox，微信发「扫歌」
自动：微信发「唱 歌名」，曲库没有就去网易云下
```

### 3. Seed-VC

```text
tools/ai/seed-vc/
  inference.py
  .venv/Scripts/python.exe
  checkpoints/
```

用 [Seed-VC](https://github.com/Plachtaa/seed-vc) 自己的方式安装。本项目不附带模型权重。

### 4. 微信指令

```text
唱 晴天
唱 晴天 副歌       只唱 30 秒
唱 晴天 整首       忽略 max_seconds
扫歌               扫描 music_inbox
```

默认只唱 60 秒（约 3~4 条语音），可在 `[sing] max_seconds` 改。

## 自动搜歌（网易云）

实现的是网页版 weapi：

```text
cloudsearch/pc               搜歌
song/enhance/player/url/v1   取播放地址
w/nuser/account/get          检查 cookie
```

- 不填 cookie：只能下免费歌
- 填 `MUSIC_U`：可以下会员歌
- 只下**完整音频**，试听片段会被跳过

获取 cookie：浏览器登录 music.163.com → F12 → 应用 → Cookie → `https://music.163.com` → 复制 `MUSIC_U` 的值。

> 歌曲版权归各平台所有，本项目不附带任何音频。

## 共享记忆（可选）

如果你同时用 Aurabot（QQ 插件），可以让微信只读它的记忆：

```ini
[memory]
enabled = true
file = C:\path\to\.dsh\plugins-data\aurabot\memories.json
owner_qq = 你的QQ号
chats = 你的微信wxid
```

只读，不写回。默认只对指定私聊生效，避免私人记忆进群。

## 目录

```text
bot.py              主程序
netease.py          网易云 weapi 客户端
memory_bridge.py    只读记忆桥
sing_convert.py     Seed-VC 换声入口
config.example.ini  配置示例
personas/           人设
docs/               架构说明
```

## 已知限制

- 微信自动化依赖 `wechatauto-replica`，微信升级可能失效
- 语音条需要前台窗口、不锁屏、管理员权限
- 微信语音条约 20 秒上限，长歌会切多条
- Seed-VC 占 3~4GB 显存
- 自动搜歌依赖网易云接口，可能随平台改动失效

## 免责声明

本项目不提供任何绕过平台限制的能力，也不鼓励批量、骚扰或未授权的声音克隆。

使用造成的账号风险、版权问题、法律责任由使用者自行承担。

## License

MIT
