# LearnLove Agent

微信聊天 AI 助手 — 本地运行，自动监听消息，生成回复建议，帮你聊得更好。

## 是什么

一个运行在你电脑上的终端程序。它直接读取微信本地数据库，实时监控指定联系人的新消息，通过 LLM 理解对话上下文和双方风格，生成 1-3 条可选回复建议，一键复制到剪贴板。

**不是为了替你聊天，而是帮你学会更好地聊天。**

## 三种模式

| 模式 | 命令 | 做什么 |
|------|------|--------|
| **回复跟进** | `/a` | 后台监听新消息 → 自动生成回复建议 → 复制到剪贴板 |
| **咨询讨论** | `/c` | 不看聊天记录，纯聊天——问策略、讨论想法、理清思路 |
| **复盘分析** | `/r` | 拉取最近聊天，用教练视角分析得失，告诉你哪里好、哪里可以更好 |

## 核心能力

- **实时监听** — 后台轮询微信数据库，新消息到达即时处理
- **语音转文字** — SILK 解码 + Whisper 本地转录（可选手动/自动模式）
- **长期记忆** — 每联系人独立归档，LLM 定期压缩，对话越多越懂你们
- **风格学习** — 分析你和对方的语言习惯，让回复更自然更像你
- **技能插件** — 内置狗头军师知识库（43 篇恋爱沟通参考），可扩展
- **复盘教练** — 回头看聊天记录，指出哪里做得好、哪里可以改进
- **跨联系人参考** — 跟 A 聊天时可以查看 B 的记忆和风格
- **28 个 LLM 工具** — 搜索消息、查询历史、管理记忆、风格分析等

## 快速开始

### 1. 安装依赖

```bash
pip install httpx zstandard pyyaml rich pyperclip
# 语音转文字（可选）
pip install pilk faster-whisper
```

### 2. 解密微信数据库

参考 `wechat-decrypt/` 目录，提取微信数据库的加密密钥。

### 3. 配置文件

```bash
cp agent/config.sample.yaml ~/.learnlove_data/config.yaml
```

编辑 `~/.learnlove_data/config.yaml`，填入：
- 微信数据库路径和密钥文件
- LLM API key（支持 DeepSeek / OpenAI 兼容接口）
- 要监听的联系人（wxid + 显示名称）

### 4. 启动

```bash
python -m agent.loop

# 自定义数据目录
python -m agent.loop --data-dir D:\MyData

# 启动即开启自动监听
python -m agent.loop --auto
```

## 技术架构

```
微信 DB (加密)
  → DBCache (解密缓存)
  → 后台监听线程 / 按需查询
  → 消息解码 (ZSTD/SILK→Whisper)
  → 上下文组装 (系统提示词 + 记忆 + 技能 + 风格 + 近期聊天)
  → LLM API (OpenAI 兼容)
  → 工具调用循环 (28 tools)
  → 回复建议 → 剪贴板
```

### 目录结构

```
LearnLove/
  agent/
    loop.py              ← 入口
    chat.py               ← REPL 主循环 + 三种模式
    llm.py                ← LLM 客户端 + 工具 schemas
    memory.py             ← 每联系人记忆系统
    context.py            ← 上下文组装器
    paths.py              ← 统一路径管理（数据/代码分离）
    style_profiler.py     ← 语言风格分析
    tool_manager.py       ← 技能插件系统
    valve.py              ← 权限门控 (L0/L1/L2)
    protocol.py           ← 消息信封
    outputs.py            ← 溢出输出管理
    tools/                ← 工具实现 (消息/联系人/语音/发送/监控)
    skills/               ← 技能插件 (狗头军师等)
    config.sample.yaml    ← 配置模板
    system_prompt.md      ← 系统提示词
  wechat-decrypt/         ← 微信数据库解密
  PRIVACY.md              ← 隐私与配置指南
```

用户数据全部存储在项目外的 `~/.learnlove_data/`，代码可安全提交 git。

## 隐私

- **所有数据存储在本地**，不会上传到任何服务器
- **代码与数据分离** — 项目目录只含代码，个人数据在独立目录
- LLM API 调用仅发送当前对话上下文（系统提示词 + 相关消息 + 工具结果）
- 详见 [PRIVACY.md](PRIVACY.md)

## 命令参考

| 命令 | 说明 |
|------|------|
| `/a` | 切换自动监听模式 |
| `/c`, `/coach` | 切换咨询模式 |
| `/r`, `/review` | 复盘分析 |
| `/peek <名称>` | 查看其他联系人的记忆/风格 |
| `/copy` | 复制最后一条建议 |
| `/send` | 自动发送（需阀门 L2） |
| `/voice <auto\|manual>` | 语音处理模式 |
| `/contact <名称>` | 切换联系人 |
| `/memory` | 查看长期记忆 |
| `/clear` | 清除对话历史 |
| `/h` | 帮助 |

## 依赖

- Python 3.11+
- 微信 Windows 客户端（数据库在本地）
- LLM API（DeepSeek / OpenAI 兼容均可）

## 致谢

本项目基于以下优秀的开源项目构建：

- **[wechat-decrypt](https://github.com/328336690/wechat-decrypt)** by [@328336690](https://github.com/328336690) — 微信 4.0 本地数据库解密工具，实现了 SQLCipher 4 密钥提取与解密
- **[chatlog-keeper](https://github.com/labazhou2024/chatlog-keeper)** by [@labazhou2024](https://github.com/labazhou2024) — 微信/QQ 聊天记录导出备份工具，支持多平台密钥提取
- **[狗头军师 (Goutoujunshi)](https://github.com/powerycy/goutoujunshi)** by [@powerycy](https://github.com/powerycy) — AI 恋爱军师 Codex Skill，提供 43 篇恋爱沟通参考与实战话术编排

感谢以上项目的作者和贡献者们 💙

## License

MIT
