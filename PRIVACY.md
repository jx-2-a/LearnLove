# Privacy & Configuration Guide

## Architecture

代码与数据完全分离：

```
LearnLove/                  ← 纯代码，可安全 git commit
  agent/                    ← Agent 源码
  wechat-decrypt/           ← 解密库
  PRIVACY.md                ← 本文件

~/.learnlove_data/           ← 所有个人数据（项目外，不可提交）
  config.yaml               ← API key + 联系人配置
  memory/<name>/            ← 聊天记忆、风格分析
  styles/ reviews/ spills/  ← 其他运行时数据
```

数据目录可通过 `--data-dir` 或环境变量 `LEARNLOVE_USER_DATA` 自定义。

## Before Committing — Checklist

提交代码前确认以下文件不含真实信息：

- [ ] `agent/config.yaml` — **不应存在**（已移到数据目录），只保留 `config.sample.yaml`
- [ ] `wechat-decrypt/all_keys.json` — gitignored
- [ ] `wechat-decrypt/decrypted/` — gitignored
- [ ] 所有 `.py` 文件不含真实 wxid、姓名、API key

## What to Configure

首次使用需要提供以下信息（都放在 `~/.learnlove_data/config.yaml`）：

| 配置项 | 说明 |
|--------|------|
| `wechat.db_dir` | 微信数据库目录路径 |
| `wechat.keys_file` | 解密密钥文件路径 |
| `llm.api_key` | LLM API key |
| `llm.model` | 模型名称 |
| `contacts` | 要监听的联系人（wxid + 名称） |

## Known Issues（未修复的残留）

以下文件在 `tools/` 和 `wechat-decrypt/` 目录中，不在 agent 内，但包含硬编码的示例路径：

- `wechat-decrypt/derive_keys.py` — 硬编码的 master key + 微信路径
- `tools/update_keys.py` — 硬编码的微信路径
- `tools/live_monitor.py`, `tools/send_reply.py` 等 — 硬编码的联系人名称作为示例

这些工具脚本未纳入 git 管理（或各自有独立的 .gitignore）。如需清理，将其中的路径和名称替换为占位符或从配置文件读取。

## Data Retention

Agent 运行时会自动生成以下数据（全部在用户数据目录中）：

- `memory/<name>/memory.md` — LLM 压缩的长期记忆
- `memory/<name>/transcript.jsonl` — 原始对话记录
- `memory/<name>/lessons.json` — 经验教训
- `memory/<name>/style.md` — 联系人语言风格分析
- `styles/user_style.md` — 用户语言风格分析
- `reviews/` — 复盘分析报告
- `live/` — 监听状态
- `spills/` — 工具调用溢出输出
- `voice_cache/` — 语音解码缓存

所有数据仅存储在本地，不会上传到任何服务器（LLM API 调用除外，仅发送当前对话上下文）。
