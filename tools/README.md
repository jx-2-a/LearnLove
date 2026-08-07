# 微信 4.1.x 数据分析工具链

## 架构总结

```
┌─────────────────────────────────────────────────┐
│  WeChat 4.1.x (Weixin.exe)                      │
│  ├── 进程: Weixin.exe (非 WeChat.exe)            │
│  ├── 核心DLL: Weixin.dll (非 WeChatWin.dll)      │
│  ├── 数据目录: D:\WeChat\Record\xwechat_files\  │
│  │   └── <wxid>\db_storage\                     │
│  │       ├── message/message_0.db ← 聊天记录     │
│  │       ├── contact/contact.db   ← 联系人       │
│  │       └── session/session.db   ← 会话列表     │
│  └── 加密: SQLCipher 4                           │
│       ├── AES-256-CBC + HMAC-SHA512              │
│       ├── page_size=4096, reserve=80             │
│       └── 密钥派生: PBKDF2-HMAC-SHA512(          │
│             master_key, salt, 256000)            │
└─────────────────────────────────────────────────┘
         │
         │ 密钥提取 (chatlog-keeper active mode)
         │ - 调试器拦截 WCDB 密码边界
         │ - 缓存在 %LOCALAPPDATA%\chatlog-keeper\
         ▼
┌─────────────────────────────────────────────────┐
│  tools/update_keys.py                           │
│  ├── 检查缓存密钥 → 有效则复用                   │
│  ├── 无效 → 调用 chatlog-keeper 提取             │
│  └── PBKDF2 派生 20 个 per-DB 密钥               │
│       → 写入 wechat-decrypt/all_keys.json        │
└─────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│  wechat-decrypt/decrypt_db.py                   │
│  ├── 逐页解密所有 .db 文件                       │
│  └── 输出到 decrypted/ 目录                      │
└─────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│  tools/extract_chat.py                          │
│  ├── 模糊搜索联系人（备注/昵称/username）          │
│  ├── ZSTD 解压消息内容                           │
│  ├── 按时间范围提取                              │
│  └── 输出 JSON + 文本预览                        │
└─────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (wechat-decrypt/mcp_server.py)      │
│  ├── get_recent_sessions()    → 最近会话         │
│  ├── get_chat_history(name)   → 聊天记录         │
│  ├── search_messages(kw)      → 全局搜索         │
│  ├── get_contacts(query)      → 搜索联系人       │
│  └── get_new_messages()       → 新消息           │
│                                                   │
│  注册: claude mcp add wechat -- python mcp_server.py│
└─────────────────────────────────────────────────┘
```

## 日常使用流程

### 更新密钥（仅在密钥过期时）

```bash
cd d:\DsEdit\LearnLove
source .venv/Scripts/activate
python tools/update_keys.py
```

密钥通常长期有效，微信不重新登录就不需要重新提取。

### 解密数据库

```bash
cd wechat-decrypt
source ../.venv/Scripts/activate
python decrypt_db.py
```

### 提取指定人的聊天

```bash
# 列出所有会话
python tools/extract_chat.py --list

# 提取最近30天
python tools/extract_chat.py --name "ta的名字" --days 30
```

### 通过 Claude Code 直接分析（MCP）

重启 Claude Code 后，直接对我说：
- "看看我微信最近谁找我"
- "帮我分析一下和 xxx 的聊天记录"
- "搜一下聊天记录里谁提到过 xxx"

## 关键发现

| 旧版微信 (3.x/4.0) | 新版微信 (4.1.10+) |
|---|---|
| WeChat.exe | Weixin.exe |
| WeChatWin.dll | Weixin.dll |
| 密钥在堆内存明文 | 密钥不在堆中，需调试器拦截 |
| 被动内存扫描即可 | 需要主动调试器模式 |
| enc_key 直接使用 | PBKDF2(password, salt, 256000) 派生 |

## 依赖工具

| 工具 | 用途 | 路径 |
|------|------|------|
| chatlog-keeper | 密钥提取（调试器模式） | chatlog-keeper/ |
| wechat-decrypt | 数据库解密 + MCP Server | wechat-decrypt/ |

## 风险提示

- 主动密钥提取（调试器模式）封号风险中等
- 被动模式（仅读内存）风险极低，但 4.1.10+ 不适用
- 密钥缓存后可长期复用，无需频繁提取
- 所有操作在你自己的电脑上完成，不联网
