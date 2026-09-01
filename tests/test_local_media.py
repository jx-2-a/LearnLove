"""LearnLove 本地媒体模型注册与运行条件测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.local_media import discover_local_media, load_media_registry


class LocalMediaTests(unittest.TestCase):
    """确保注册表可扩展，且缺少任一前置都不会误报就绪。"""

    def _write_registry(self, root: Path) -> Path:
        """创建只含测试路径的最小双能力注册表。"""
        registry = {
            "version": 1,
            "defaults": {
                "speech_to_text": "speech-test",
                "image_understanding": "vision-test",
            },
            "models": {
                "speech-test": {
                    "capability": "speech_to_text",
                    "backend": "funasr_sensevoice_cli",
                    "model_path": "models/speech.gguf",
                    "runner_path": "runtime/speech.exe",
                    "enabled": True,
                },
                "vision-test": {
                    "capability": "image_understanding",
                    "backend": "llama_mtmd_cli",
                    "model_path": "models/vision.gguf",
                    "projector_path": "models/mmproj.gguf",
                    "runner_path": "runtime/vision.exe",
                    "enabled": True,
                },
            },
        }
        path = root / "registry.json"
        path.write_text(json.dumps(registry), encoding="utf-8")
        return path

    def test_missing_runner_and_projector_are_reported(self):
        """模型存在但执行器或投影缺失时保持未就绪。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "models" / "speech.gguf").touch()
            (root / "models" / "vision.gguf").touch()
            registry = self._write_registry(root)
            with patch("agent.local_media.importlib.util.find_spec", return_value=object()):
                status = discover_local_media(root, registry)
        self.assertFalse(status["speech_to_text"]["ready"])
        self.assertFalse(status["image_understanding"]["ready"])
        self.assertIn("命令行执行器", status["speech_to_text"]["missing"])
        self.assertIn("多模态投影文件", status["image_understanding"]["missing"])

    def test_all_required_files_make_backends_ready(self):
        """模型、执行器、解码器与投影齐全时才标为就绪。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            runtime = root / "runtime"
            models.mkdir()
            runtime.mkdir()
            for name in ("speech.gguf", "vision.gguf", "mmproj.gguf"):
                (models / name).touch()
            (runtime / "speech.exe").touch()
            (runtime / "vision.exe").touch()
            registry = self._write_registry(root)
            with patch("agent.local_media.importlib.util.find_spec", return_value=object()):
                status = discover_local_media(root, registry)
        self.assertTrue(status["speech_to_text"]["ready"])
        self.assertTrue(status["image_understanding"]["ready"])

    def test_registry_accepts_additional_models(self):
        """同一能力可增加非默认模型而不修改发现代码。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = self._write_registry(root)
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            data["models"]["speech-extra"] = {
                **data["models"]["speech-test"],
                "model_path": "models/extra.gguf",
            }
            registry_path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_media_registry(registry_path)
            status = discover_local_media(root, registry_path)
        self.assertIn("speech-extra", loaded["models"])
        self.assertIn("speech-extra", status["models"])


if __name__ == "__main__":
    unittest.main()
