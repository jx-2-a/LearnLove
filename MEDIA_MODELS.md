# LearnLove 本地媒体模型

模型、视觉投影、EXE、DLL、下载包和本地构建目录全部位于 `models/`、`runtime/`，已由 `.gitignore` 排除。仓库只提交模型注册表、适配代码、来源和校验值，因此正常提交不会携带数 GB 二进制文件。

## 当前默认模型

语音默认使用 `sensevoice-small-q8-official`。模型来自 [FunAudioLLM/SenseVoiceSmall-GGUF](https://huggingface.co/FunAudioLLM/SenseVoiceSmall-GGUF)，许可证为 Apache-2.0；运行器来自 [QwenAudio/SenseVoice runtime-llamacpp-v0.1.9](https://github.com/QwenAudio/SenseVoice/releases/tag/runtime-llamacpp-v0.1.9)。本机模型 SHA-256 为 `4ae45c94422de949b387e2e0fb10d7e14e4c42c69db30c3444ecc7d4b844b7c5`，运行时压缩包 SHA-256 为 `6767af74e42c8b928742e12d5995c139636d9482ea151cdbb51f1b7573667772`。

旧版 `sense-voice-small-q4_0.gguf` 与它的 SenseVoice.cpp 本地构建前置曾因 Windows CPU 推理访问冲突而保留为备用；现已按用户要求删除，不再出现在模型注册表中。

图片默认使用 `qwen2.5-vl-3b-q4-k-m`。模型与投影均来自 [ggml-org/Qwen2.5-VL-3B-Instruct-GGUF](https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF)，许可证为 Apache-2.0；运行器来自 [llama.cpp b10721](https://github.com/ggml-org/llama.cpp/releases/tag/b10721)。模型 SHA-256 为 `d02fe9b69ad8cadbbd228e387667af66612c44bed29ffc8eb1e7caf9ac486c12`，投影 SHA-256 为 `980c9b2f78c04e6cff93d277ada09e768394f112d75db3b4e9dea8a69f9fb904`，运行时压缩包 SHA-256 为 `7e20817d2b24164bd873c5d0747bd4b85f056e11ec3edeab503bcb61181a7273`。

## 增加或切换模型

在 `agent/media_models.json` 的 `models` 中增加唯一 ID，填写 `capability`、`backend`、`model_path`、`runner_path` 和需要的 `projector_path`。目前支持 `funasr_sensevoice_cli`、`sensevoice_cpp_cli` 和 `llama_mtmd_cli`。模型文件仍放在已忽略目录，不要放进 `agent/`。

修改注册表的 `defaults` 可以切换默认模型；也可以只在本机设置 `LEARNLOVE_SPEECH_MODEL` 或 `LEARNLOVE_VISION_MODEL`。如果要使用另一份注册表，设置 `LEARNLOVE_MEDIA_REGISTRY`。业务层只按 `speech_to_text` 和 `image_understanding` 能力取处理器，不依赖具体模型名。

## 已验证结果

SenseVoice 官方 q8 已对一条 6.14 秒的真实微信 SILK 语音完成 `SILK -> WAV -> 本地转写`，进程退出码为 0。Qwen2.5-VL 已对包含红色矩形和 `TEST 42` 的本地测试图完成识别并返回 `TEST 42`，进程退出码为 0。测试文件位于已忽略的 `data/`，不会进入提交。
