"""LearnLove 本地媒体模型注册、状态检查与命令行处理器。"""

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = Path(__file__).with_name("media_models.json")
CAPABILITIES = ("speech_to_text", "image_understanding")
MODEL_ENV = {
    "speech_to_text": "LEARNLOVE_SPEECH_MODEL",
    "image_understanding": "LEARNLOVE_VISION_MODEL",
}


def load_media_registry(path: Path | str | None = None) -> dict:
    """读取可提交的模型注册表，并验证最小结构。"""
    registry_path = Path(
        path or os.environ.get("LEARNLOVE_MEDIA_REGISTRY", DEFAULT_REGISTRY)
    )
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("models"), dict):
        raise ValueError(f"媒体模型注册表格式无效: {registry_path}")
    return data


def _resolve_path(value: str, root: Path) -> Path:
    """将注册表中的相对路径限制在指定项目根目录解释。"""
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else root / path


def _silk_decoder_available() -> bool:
    """检查微信 SILK 转 WAV 所需的 Python 解码器。"""
    return (
        importlib.util.find_spec("pilk") is not None
        or importlib.util.find_spec("pysilk") is not None
    )


def _selected_id(registry: dict, capability: str) -> str:
    """按环境变量覆盖或注册表默认值选择模型。"""
    return os.environ.get(MODEL_ENV[capability], registry["defaults"].get(capability, ""))


def _model_status(model_id: str, config: dict, root: Path) -> dict:
    """检查单个注册项的模型、投影和执行器文件。"""
    capability = config.get("capability", "")
    model = _resolve_path(config.get("model_path", ""), root)
    runner = _resolve_path(config.get("runner_path", ""), root)
    projector_value = config.get("projector_path", "")
    projector = _resolve_path(projector_value, root) if projector_value else None
    missing = []
    if not model.is_file():
        missing.append("模型文件")
    if not runner.is_file():
        missing.append("命令行执行器")
    if projector is not None and not projector.is_file():
        missing.append("多模态投影文件")
    silk_decoder = _silk_decoder_available() if capability == "speech_to_text" else None
    if capability == "speech_to_text" and not silk_decoder:
        missing.append("微信 SILK 解码器 pilk/pysilk")
    enabled = bool(config.get("enabled", True))
    if not enabled:
        missing.append(config.get("disabled_reason", "注册项已停用"))
    return {
        "id": model_id,
        "capability": capability,
        "backend": config.get("backend", ""),
        "enabled": enabled,
        "model": model.name,
        "model_path": str(model),
        "model_found": model.is_file(),
        "runner_path": str(runner),
        "runner_found": runner.is_file(),
        "projector_path": str(projector) if projector else "",
        "projector_found": projector.is_file() if projector else None,
        "silk_decoder_found": silk_decoder,
        "ready": not missing,
        "missing": missing,
    }


def discover_local_media(
    root: Path | str | None = None,
    registry_path: Path | str | None = None,
) -> dict:
    """返回默认能力状态及所有已注册本地模型的可用性。"""
    project_root = Path(root or PROJECT_ROOT)
    registry = load_media_registry(registry_path)
    models = {
        model_id: _model_status(model_id, config, project_root)
        for model_id, config in registry["models"].items()
    }
    selected = {}
    for capability in CAPABILITIES:
        model_id = _selected_id(registry, capability)
        selected[capability] = models.get(model_id, {
            "id": model_id,
            "capability": capability,
            "ready": False,
            "missing": ["注册表中不存在所选模型"],
        })
    return {
        "root_available": project_root.is_dir(),
        "registry": str(Path(registry_path or DEFAULT_REGISTRY)),
        "speech_to_text": selected["speech_to_text"],
        "image_understanding": selected["image_understanding"],
        "models": models,
    }


def _decode_silk(source: Path, target: Path, sample_rate: int = 24000) -> None:
    """把微信 SILK 原始数据解码成单声道 16-bit WAV。"""
    from pilk import decode

    pcm_path = target.with_suffix(".pcm")
    decode(str(source), str(pcm_path))
    pcm = pcm_path.read_bytes()
    if not pcm:
        raise RuntimeError("SILK 解码结果为空")
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


def _run(command: list[str], cwd: Path, timeout: int) -> str:
    """执行本地模型命令，失败时保留可诊断输出。"""
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"本地模型退出码 {completed.returncode}: {detail[-2000:]}"
        )
    text = completed.stdout.strip()
    if not text:
        text = completed.stderr.strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"<\|[^|]+\|>", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [
        line for line in lines
        if not line.startswith(("[sensevoice]", "system_info:", "main:"))
    ]
    result = "\n".join(lines).strip()
    if not result:
        raise RuntimeError("本地模型执行成功但没有返回可归档文本")
    return result


def _speech_handler(config: dict, root: Path) -> Callable[[dict], str]:
    """创建 SenseVoice/FunASR 语音任务处理器。"""
    model = _resolve_path(config["model_path"], root)
    runner = _resolve_path(config["runner_path"], root)
    backend = config["backend"]

    def handle(job: dict) -> str:
        """解码一个已归档语音并返回转写正文。"""
        source = Path(job["archived_path"])
        if not source.is_file():
            raise FileNotFoundError(f"语音归档不存在: {source}")
        with tempfile.TemporaryDirectory(prefix="learnlove-stt-") as temp_dir:
            audio = source
            if source.suffix.lower() == ".silk":
                audio = Path(temp_dir) / "audio.wav"
                _decode_silk(source, audio, int(config.get("sample_rate", 24000)))
            if backend == "funasr_sensevoice_cli":
                command = [str(runner), "-m", str(model), "-a", str(audio)]
            elif backend == "sensevoice_cpp_cli":
                command = [
                    str(runner), "-m", str(model), "-f", str(audio),
                    "-t", str(config.get("threads", 4)), "-ng", "-nt", "-np",
                ]
            else:
                raise ValueError(f"不支持的语音后端: {backend}")
            return _run(command, runner.parent, int(config.get("timeout", 300)))

    return handle


def _vision_handler(config: dict, root: Path) -> Callable[[dict], str]:
    """创建 llama.cpp mtmd 图片理解任务处理器。"""
    model = _resolve_path(config["model_path"], root)
    projector = _resolve_path(config["projector_path"], root)
    runner = _resolve_path(config["runner_path"], root)

    def handle(job: dict) -> str:
        """理解一个已解码图片并返回客观描述。"""
        image = Path(job["archived_path"])
        if not image.is_file():
            raise FileNotFoundError(f"图片归档不存在: {image}")
        if image.suffix.lower() == ".dat" or job.get("mime_type") == "application/octet-stream":
            raise RuntimeError("图片仍是微信加密 .dat，需先解码后再识图")
        command = [
            str(runner), "-m", str(model), "--mmproj", str(projector),
            "--image", str(image), "-p", config.get("prompt", "请完整描述图片。"),
            "-n", str(config.get("max_tokens", 512)), "--temp", "0",
        ]
        if config.get("cpu_only", True):
            command.extend(["-ngl", "0", "--no-mmproj-offload"])
        return _run(command, runner.parent, int(config.get("timeout", 900)))

    return handle


def register_local_media_providers(existing: set[str] | None = None) -> dict:
    """把就绪的默认本地模型注册到统一媒体任务接口。"""
    from agent.media_api import register_media_provider

    existing = existing or set()
    registry = load_media_registry()
    status = discover_local_media()
    registered = {}
    for capability in CAPABILITIES:
        selected = status[capability]
        if capability in existing or not selected.get("ready"):
            continue
        model_id = selected["id"]
        config = registry["models"][model_id]
        if capability == "speech_to_text":
            handler = _speech_handler(config, PROJECT_ROOT)
        else:
            handler = _vision_handler(config, PROJECT_ROOT)
        provider_name = f"local:{model_id}"
        register_media_provider(capability, provider_name, handler)
        registered[capability] = provider_name
    return registered
