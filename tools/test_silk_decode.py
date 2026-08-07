"""
测试 SILK V3 音频解码 + Whisper 转录全链路
从 media_0.db VoiceInfo 表提取音频 → pilk 解码 WAV → faster-whisper 转录
"""
import sqlite3
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted", "message", "media_0.db")


def check_voice_table():
    """检查 VoiceInfo 表结构和数据"""
    conn = sqlite3.connect(DB_PATH)

    # 表结构
    print("=== VoiceInfo 表结构 ===")
    cols = conn.execute("PRAGMA table_info(VoiceInfo)").fetchall()
    for c in cols:
        print(f"  {c[1]:20s} {c[2]:10s}")

    # chat_name_id 分布
    print("\n=== chat_name_id 分布 ===")
    rows = conn.execute(
        "SELECT chat_name_id, COUNT(*) as cnt, COUNT(voice_data) as has_data "
        "FROM VoiceInfo GROUP BY chat_name_id ORDER BY cnt DESC"
    ).fetchall()
    for r in rows:
        print(f"  chat_name_id={r[0]:3d}  总数={r[1]:4d}  有voice_data={r[2]:4d}")

    return conn


def extract_sample_audio(conn, chat_name_id=None, limit=1):
    """提取样本音频数据"""
    if chat_name_id:
        rows = conn.execute(
            "SELECT chat_name_id, create_time, local_id, svr_id, voice_data, length(voice_data) as data_len "
            "FROM VoiceInfo WHERE voice_data IS NOT NULL AND chat_name_id=? "
            "ORDER BY create_time DESC LIMIT ?",
            (chat_name_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chat_name_id, create_time, local_id, svr_id, voice_data, length(voice_data) as data_len "
            "FROM VoiceInfo WHERE voice_data IS NOT NULL ORDER BY create_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return rows


def test_silk_decode(voice_data, output_path):
    """用 pilk 解码 SILK V3 为 WAV"""
    import tempfile
    try:
        from pilk import decode

        # pilk.decode(silk_path, pcm_path) → (duration_ms, pcm_data)
        # 需要两个文件路径: silk输入, pcm输出
        with tempfile.NamedTemporaryFile(suffix='.silk', delete=False) as tmp:
            tmp.write(voice_data)
            silk_path = tmp.name

        pcm_tmp = tempfile.NamedTemporaryFile(suffix='.pcm', delete=False)
        pcm_path = pcm_tmp.name
        pcm_tmp.close()

        try:
            # pilk.decode(silk_path, pcm_path) → int (status code)
            # PCM 数据写入 pcm_path 文件
            result = decode(silk_path, pcm_path)
            print(f"  [pilk] 解码返回: {result}")

            # 读取 PCM 数据
            with open(pcm_path, 'rb') as f:
                pcm_data = f.read()

            # 通过 pilk.get_duration 获取时长
            from pilk import get_duration
            duration = get_duration(silk_path)
            print(f"  [pilk] 解码成功! 时长={duration:.1f}ms, PCM={len(pcm_data)} 字节")

            # 写入 WAV 文件
            import wave
            with wave.open(output_path, 'wb') as wf:
                wf.setnchannels(1)          # 单声道
                wf.setsampwidth(2)           # 16-bit
                wf.setframerate(24000)       # SILK 采样率通常是 24000
                wf.writeframes(pcm_data)

            print(f"  [WAV] 已保存: {output_path}")
            return duration, pcm_data
        finally:
            os.unlink(silk_path)
            if os.path.exists(pcm_path):
                os.unlink(pcm_path)

    except Exception as e:
        print(f"  [ERROR] pilk 解码失败: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def test_whisper_transcribe(wav_path):
    """用 faster-whisper 转录 WAV"""
    try:
        from faster_whisper import WhisperModel
        import time

        print(f"  [Whisper] 加载模型 tiny (GPU加速)...")
        t0 = time.time()
        model = WhisperModel("tiny", device="cuda", compute_type="float16")
        print(f"  [Whisper] 模型加载完成, 耗时 {time.time()-t0:.1f}s")

        t0 = time.time()
        segments, info = model.transcribe(wav_path, language="zh", beam_size=5)
        print(f"  [Whisper] 检测语言: {info.language} (概率={info.language_probability:.2f})")

        text_parts = []
        for seg in segments:
            text_parts.append(seg.text)
            print(f"  [Whisper] 片段 [{seg.start:.1f}s-{seg.end:.1f}s]: {seg.text}")

        full_text = "".join(text_parts)
        print(f"  [Whisper] 转录完成, 耗时 {time.time()-t0:.1f}s")
        print(f"  [Whisper] 完整文本: {full_text}")
        return full_text

    except Exception as e:
        print(f"  [ERROR] Whisper 转录失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    print("=" * 60)
    print("  SILK V3 → WAV → Whisper 全链路测试")
    print("=" * 60)

    conn = check_voice_table()

    # 先测试 filehelper 的语音 (chat_name_id=5, 用户自己发的测试语音)
    print("\n=== 提取 filehelper 测试语音 ===")
    samples = extract_sample_audio(conn, chat_name_id=5, limit=3)
    if not samples:
        print("  filehelper 没有语音, 改用 chat_name_id=1 (联系人名称)")
        samples = extract_sample_audio(conn, chat_name_id=1, limit=1)

    if not samples:
        print("  [ERROR] 没有找到任何语音数据!")
        conn.close()
        return

    for i, (chat_id, ts, local_id, svr_id, voice_data, data_len) in enumerate(samples):
        print(f"\n--- 样本 {i+1}: chat={chat_id} local_id={local_id} data_len={data_len} ---")

        # 验证 SILK 魔数
        if voice_data:
            magic = voice_data[:10]
            print(f"  前10字节: {magic}")
            is_silk = b"#!SILK_V3" in voice_data[:20]
            print(f"  是 SILK_V3: {is_silk}")

            if not is_silk:
                print(f"  完整头部: {voice_data[:32]}")
                continue

            # 解码
            output_dir = os.path.join(PROJECT_ROOT, "data", "voice_test")
            os.makedirs(output_dir, exist_ok=True)
            wav_path = os.path.join(output_dir, f"test_{chat_id}_{local_id}.wav")

            duration, pcm = test_silk_decode(voice_data, wav_path)

            if duration and duration > 0:
                # 转录
                print("\n  --- Whisper 转录 ---")
                text = test_whisper_transcribe(wav_path)

                if text:
                    print(f"\n  {'='*40}")
                    print(f"  [转录结果] {text}")
                    print(f"  {'='*40}")

            break  # 只测第一条

    conn.close()


if __name__ == "__main__":
    main()
