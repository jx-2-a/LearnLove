"""文件驱动的模型档案，供终端模式和 AgentHub Web 模式共用。"""

import copy
import os
from pathlib import Path

import yaml


_MODEL_FILE = Path(__file__).with_name("models.yaml")
_DEFAULTS = {
    "provider": "custom",
    "protocol": "openai",
    "api_base": "",
    "api_key": "",
    "model": "",
    "thinking": False,
    "reasoning_effort": "",
    "temperature": 0.7,
    "max_tokens": 4096,
    "max_context_tokens": 0,
    "params": ["temperature", "max_tokens"],
}


class ModelRegistry:
    def __init__(self, path=None):
        """加载模型档案；path 主要用于测试或自定义目录。"""
        self.path = Path(path or _MODEL_FILE)
        self.default = ""
        self.profiles = {}
        self.load()

    def load(self):
        """从 YAML 重载模型档案，并从环境变量读取密钥。"""
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        data = data or {}
        self.default = str(data.get("default", "") or "")
        self.profiles = {}
        for key, raw in (data.get("models", {}) or {}).items():
            profile = copy.deepcopy(_DEFAULTS)
            profile.update(raw or {})
            profile["key"] = key
            env_name = str(profile.get("api_key_env", "") or "")
            profile["api_key"] = os.environ.get(env_name, "") or profile.get("api_key", "")
            self.profiles[key] = profile
        if not self.default and self.profiles:
            self.default = next(iter(self.profiles))
        return self

    def resolve_key(self, requested=""):
        """把档案键或真实模型名解析成档案键。"""
        requested = str(requested or self.default)
        if requested in self.profiles:
            return requested
        for key, profile in self.profiles.items():
            if requested == profile.get("model"):
                return key
        return self.default

    def profile(self, requested=""):
        """返回独立的模型档案副本，避免运行时设置污染注册表。"""
        key = self.resolve_key(requested)
        profile = copy.deepcopy(self.profiles.get(key, _DEFAULTS))
        profile["model_profile"] = key
        return profile

    def resolve(self, llm_config=None):
        """以模型档案为基线，再兼容旧 config.yaml 中的显式字段。"""
        config = dict(llm_config or {})
        requested = (config.get("model_profile")
                     or os.environ.get("LEARNLOVE_MODEL_PROFILE", "")
                     or config.get("model"))
        profile = self.profile(requested)
        for field in (
            "provider", "protocol", "api_base", "api_key", "model", "thinking",
            "reasoning_effort", "temperature", "max_tokens", "max_context_tokens",
        ):
            if field in config and config[field] not in (None, ""):
                profile[field] = config[field]
        return profile


def load_registry(path=None):
    """新建并返回注册表，确保编辑 models.yaml 后下次调用即可生效。"""
    return ModelRegistry(path)


def resolve_llm_config(config, path=None):
    """解析一段 llm 配置，保留旧配置格式的向后兼容。"""
    return load_registry(path).resolve(config)
