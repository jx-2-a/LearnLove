"""
消息解码工具 — 文字解码 (zstd) + 语音解码 (SILK→Whisper)
"""

import os
import json
import hashlib
import sqlite3
import tempfile
import wave

from agent.protocol import ok, err
from agent.tools._state import state


def decode_text(content, ct: int) -> str:
    """解码消息内容：ZSTD 解压或 UTF-8 解码"""
    import zstandard as zstd
    dctx = zstd.ZstdDecompressor()
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return content.decode('utf-8', errors='replace')
    elif isinstance(content, bytes):
        return content.decode('utf-8', errors='replace')
    else:
        return str(content) if content else ""


# ---- 语音缓存 ----

def _voice_cache_path(voice_data: bytes) -> str:
    """返回语音缓存的 JSON 文件路径"""
    md5 = hashlib.md5(voice_data).hexdigest()
    cache_dir = state.voice_cache_dir
    return os.path.join(cache_dir, f"{md5}.json")


def _voice_cache_get(voice_data: bytes) -> str | None:
    """检查缓存，命中返回文本"""
    cache_path = _voice_cache_path(voice_data)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                return cached.get("text", "")
        except Exception:
            pass
    return None


def _voice_cache_set(voice_data: bytes, text: str, duration_ms: int = 0):
    """写入语音缓存"""
    os.makedirs(state.voice_cache_dir, exist_ok=True)
    cache_path = _voice_cache_path(voice_data)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"text": text, "duration_ms": duration_ms}, f, ensure_ascii=False)


# ---- 语音解码 ----

def decode_voice(contact_name: str, message_ts: float) -> dict:
    """手动解码单条语音消息。从 media_0.db 获取 voice_data，SILK→WAV→Whisper。

    Returns {ok, data: {text, duration_ms, cached: bool}}
    """
    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")

    voice_mode = state.voice_mode(wxid)
    if voice_mode == "manual":
        return ok({
            "text": "[语音待转]",
            "note": "当前为手动模式，请用户口头描述语音内容或切换到 auto 模式",
        })

    media_path = state.db_cache.get_media_db_path()
    if not media_path:
        return err("无法访问 media_0.db")

    conn = sqlite3.connect(media_path)
    row = conn.execute(
        "SELECT voice_data, duration FROM VoiceInfo WHERE create_time = ? AND voice_data IS NOT NULL",
        (message_ts,)
    ).fetchone()
    conn.close()

    if not row:
        return err(f"未找到语音数据 (create_time={message_ts})")

    voice_data, duration = row
    if b"#!SILK_V3" not in voice_data[:20]:
        return err("非 SILK V3 格式语音")

    # 检查缓存
    cached = _voice_cache_get(voice_data)
    if cached:
        return ok({"text": cached, "duration_ms": duration or 0, "cached": True})

    # 转录
    model_name = state.whisper_model_name(wxid)
    try:
        text, duration_ms = _transcribe_silk(voice_data, model_name)
        _voice_cache_set(voice_data, text, duration_ms)
        return ok({"text": text, "duration_ms": duration_ms, "cached": False})
    except Exception as e:
        return err(f"语音转录失败: {e}")


def batch_decode_voices(messages: list[dict], contact_name: str) -> dict:
    """批量解码消息列表中的语音消息

    Returns {ok, data: {decoded: int, messages: [...]}}
    """
    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")

    voice_mode = state.voice_mode(wxid)
    voice_msgs = [(i, m) for i, m in enumerate(messages) if m.get("local_type") == 34]
    if not voice_msgs:
        return ok({"decoded": 0, "messages": messages, "note": "无语音消息"})

    if voice_mode == "manual":
        for i, m in voice_msgs:
            messages[i]["voice_text"] = "[语音待转]"
        return ok({
            "decoded": 0,
            "messages": messages,
            "note": f"手动模式，{len(voice_msgs)} 条语音标记为待转",
        })

    # auto 模式
    media_path = state.db_cache.get_media_db_path()
    if not media_path:
        return err("无法访问 media_0.db")

    conn = sqlite3.connect(media_path)
    times = [m["create_time"] for _, m in voice_msgs]
    placeholders = ",".join("?" * len(times))
    rows = conn.execute(
        f"SELECT create_time, voice_data, duration FROM VoiceInfo "
        f"WHERE create_time IN ({placeholders}) AND voice_data IS NOT NULL",
        times
    ).fetchall()
    voice_data_map = {}
    for ts, vd, dur in rows:
        voice_data_map[ts] = (vd, dur)
    conn.close()

    model_name = state.whisper_model_name(wxid)
    decoded_count = 0

    for i, m in voice_msgs:
        vd_dur = voice_data_map.get(m["create_time"])
        if not vd_dur:
            messages[i]["voice_text"] = "(无音频)"
            continue

        vd, dur = vd_dur
        if b"#!SILK_V3" not in vd[:20]:
            messages[i]["voice_text"] = "(非SILK)"
            continue

        # 检查缓存
        cached = _voice_cache_get(vd)
        if cached:
            messages[i]["voice_text"] = cached
            messages[i]["voice_duration_ms"] = dur or 0
            decoded_count += 1
            continue

        try:
            text, duration_ms = _transcribe_silk(vd, model_name)
            messages[i]["voice_text"] = text
            messages[i]["voice_duration_ms"] = duration_ms
            _voice_cache_set(vd, text, duration_ms)
            decoded_count += 1
        except Exception as e:
            messages[i]["voice_text"] = f"(转录失败: {e})"

    return ok({"decoded": decoded_count, "messages": messages})


def set_voice_mode(contact_name: str, mode: str) -> dict:
    """切换联系人的语音处理模式

    Returns {ok, data: {contact, voice_mode}}
    """
    if mode not in ("auto", "manual"):
        return err("mode 必须是 auto 或 manual")

    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")

    # 更新内存中的配置
    for c in state.config.get("contacts", []):
        if c.get("wxid") == wxid:
            c["voice_mode"] = mode
            break

    return ok({"contact": display, "voice_mode": mode})


# ---- 内部: SILK → Whisper ----

_whisper_model = None
_whisper_model_name = None

def _transcribe_silk(voice_data: bytes, model_name: str = "small") -> tuple[str, int]:
    """SILK V3 → PCM → WAV → Whisper 转录。返回 (text, duration_ms)。

    延迟加载 Whisper 模型（首次调用时加载到 GPU）。
    """
    global _whisper_model, _whisper_model_name
    from pilk import decode as silk_decode

    with tempfile.TemporaryDirectory() as tmpdir:
        silk_path = os.path.join(tmpdir, "voice.silk")
        pcm_path = os.path.join(tmpdir, "voice.pcm")
        wav_path = os.path.join(tmpdir, "voice.wav")

        # SILK → PCM
        with open(silk_path, "wb") as f:
            f.write(voice_data)
        silk_decode(silk_path, pcm_path)

        # PCM → WAV (24kHz, mono, 16-bit)
        with open(pcm_path, "rb") as f:
            pcm_data = f.read()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm_data)

        # 获取时长
        frame_count = len(pcm_data) // 2
        duration_ms = int(frame_count / 24000 * 1000)

        # Whisper 转录（延迟加载）
        if _whisper_model is None or _whisper_model_name != model_name:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(model_name, device="cuda", compute_type="float16")
            _whisper_model_name = model_name

        segments, info = _whisper_model.transcribe(wav_path, language="zh", beam_size=5)
        text = "".join(seg.text for seg in segments)

        return text, duration_ms
