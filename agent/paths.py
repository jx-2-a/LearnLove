"""
统一路径模块 — 所有用户数据路径由此派生。

使用方式:
    from agent.paths import memory_dir, config_path, ...

数据根目录优先级:
    1. 环境变量 LEARNLOVE_USER_DATA
    2. 命令行参数 --data-dir（由 loop.py 调用 set_user_data_dir 设置）
    3. 默认 ~/.learnlove_data

目录结构:
    {USER_DATA_DIR}/
      config.yaml
      memory/{name}/     memory.md, transcript.jsonl, archive/, style.md, lessons.json, notes.json
      styles/            user_style.md, user_style_meta.json
      spills/            溢出输出
      reviews/           复盘报告
      live/              监听状态
      voice_cache/       语音缓存
"""

import os

_user_data_dir: str | None = None


def set_user_data_dir(path: str):
    """设置用户数据根目录（由 loop.py 在启动时调用）。"""
    global _user_data_dir
    _user_data_dir = os.path.abspath(path)
    os.makedirs(_user_data_dir, exist_ok=True)


def get_user_data_dir() -> str:
    """获取用户数据根目录。"""
    global _user_data_dir
    if _user_data_dir is None:
        d = os.environ.get("LEARNLOVE_USER_DATA", os.path.expanduser("~/.learnlove_data"))
        set_user_data_dir(d)
    return _user_data_dir


# ---- 派生路径函数 ----


def config_path() -> str:
    return os.path.join(get_user_data_dir(), "config.yaml")


def memory_dir(name: str = "") -> str:
    """每联系人记忆目录。name 为空时返回 memory/ 根目录。"""
    if name:
        return os.path.join(get_user_data_dir(), "memory", name)
    return os.path.join(get_user_data_dir(), "memory")


def memory_md_path(name: str) -> str:
    return os.path.join(memory_dir(name), "memory.md")


def global_memory_dir() -> str:
    """返回服务于用户本人、非任何联系人的全局记忆目录。"""
    return os.path.join(get_user_data_dir(), "memory", "_global")


def global_memory_path() -> str:
    """返回可注入所有对话的用户全局记忆文件。"""
    return os.path.join(global_memory_dir(), "memory.md")


def global_memory_transcript_path() -> str:
    """返回全局记忆的待整理原始记录文件。"""
    return os.path.join(global_memory_dir(), "transcript.jsonl")


def media_preferences_path() -> str:
    """返回本机持久化的媒体模式设置文件。"""
    return os.path.join(live_dir(), "media_preferences.json")


def transcript_path(name: str) -> str:
    return os.path.join(memory_dir(name), "transcript.jsonl")


def archive_dir(name: str) -> str:
    return os.path.join(memory_dir(name), "archive")


def contact_style_path(name: str) -> str:
    return os.path.join(memory_dir(name), "style.md")


def lessons_path(name: str) -> str:
    return os.path.join(memory_dir(name), "lessons.json")


def notes_path(name: str) -> str:
    """重要内容留档（时间点快照，按需读取，永不注入）。"""
    return os.path.join(memory_dir(name), "notes.json")


def styles_dir() -> str:
    return os.path.join(get_user_data_dir(), "styles")


def user_style_path() -> str:
    return os.path.join(styles_dir(), "user_style.md")


def user_style_meta_path() -> str:
    return os.path.join(styles_dir(), "user_style_meta.json")


def spills_dir() -> str:
    return os.path.join(get_user_data_dir(), "spills")


def reviews_dir() -> str:
    return os.path.join(get_user_data_dir(), "reviews")


def analysis_stage_dir(name: str = "") -> str:
    """返回联系人阶段分析底稿目录，和原始消息、事件、复盘分层保存。"""
    root = os.path.join(get_user_data_dir(), "analysis_stages")
    safe_name = str(name or "自己").replace("/", "_").replace("\\", "_")
    return os.path.join(root, safe_name)


def contact_reviews_dir(name: str) -> str:
    """返回某联系人的独立复盘报告目录。"""
    safe_name = str(name or "未分类").replace("/", "_").replace("\\", "_")
    return os.path.join(reviews_dir(), safe_name)


def live_dir() -> str:
    return os.path.join(get_user_data_dir(), "live")


def live_feed_path() -> str:
    return os.path.join(live_dir(), "incoming.jsonl")


def monitor_state_path() -> str:
    return os.path.join(live_dir(), "monitor_state.json")


def context_json_path(name: str) -> str:
    """每联系人的会话上下文（agent <-> 用户对话历史），用于重启恢复。"""
    return os.path.join(memory_dir(name), "context.json")


def review_state_path(name: str) -> str:
    """复盘进度文件 — 记录上次复盘的时间戳和摘要，用于增量分析。"""
    return os.path.join(memory_dir(name), "review_state.json")


def voice_cache_dir() -> str:
    return os.path.join(get_user_data_dir(), "voice_cache")


def records_db_path() -> str:
    """统一事实归档库路径。"""
    return os.path.join(get_user_data_dir(), "archive", "records.db")


def archived_media_dir() -> str:
    """语音、图片等原始媒体的长期归档目录。"""
    return os.path.join(get_user_data_dir(), "archive", "media")


# ---- 目录创建 ----


def ensure_dirs():
    """确保所有必要的子目录存在。"""
    dirs = [styles_dir(), spills_dir(), reviews_dir(), live_dir(), voice_cache_dir(),
            archived_media_dir()]
    # memory_dir 按需创建，这里只建根目录
    memory_root = memory_dir()
    dirs.append(memory_root)
    for d in dirs:
        os.makedirs(d, exist_ok=True)
