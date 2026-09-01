"""语音归档与可插拔处理队列工具。"""

from agent.media import archive_voice
from agent.protocol import err, ok
from agent.tools._state import state


def decode_text(content, ct: int) -> str:
    """兼容旧调用的 ZSTD/UTF-8 文本解码。"""
    import zstandard as zstd
    if isinstance(content,bytes):
        if ct == 4:
            try:
                return zstd.ZstdDecompressor().decompress(content).decode("utf-8",errors="replace")
            except Exception:
                pass
        return content.decode("utf-8",errors="replace")
    return str(content) if content else ""


def decode_voice(contact_name: str, message_ts: float) -> dict:
    """兼容旧工具名：归档语音并排入已注册的自动处理器。"""
    wxid,display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")
    message = {"message_id":f"voice:{wxid}:{message_ts}","contact_wxid":wxid,
               "contact_name":display,"create_time":message_ts,
               "local_type":34,"metadata":{}}
    result = archive_voice(message)
    if result.get("error"):
        return err(result["error"])
    return ok({**result,"text":"[语音待转写]",
               "note":"原始语音已归档；本地或远程处理器就绪后可补跑"})


def batch_decode_voices(messages: list[dict], contact_name: str) -> dict:
    """批量归档语音并加入处理队列。"""
    queued = 0
    for message in messages:
        if message.get("local_type") != 34:
            continue
        result = archive_voice(message)
        message["voice_text"] = "[语音待转写]"
        if result.get("job_id"):
            queued += 1
    return ok({"decoded":0,"queued":queued,"messages":messages})


def set_voice_mode(mode: str, contact_name: str = "") -> dict:
    """设置服务用户本人的全局语音处理模式，保留旧参数兼容。"""
    try:
        selected = state.set_media_mode("voice", mode)
    except ValueError as exc:
        return err(str(exc))
    return ok({"voice_mode":selected,"scope":"global"})


def set_image_mode(mode: str) -> dict:
    """设置服务用户本人的全局图片理解模式。"""
    try:
        selected = state.set_media_mode("image", mode)
    except ValueError as exc:
        return err(str(exc))
    return ok({"image_mode":selected,"scope":"global"})
