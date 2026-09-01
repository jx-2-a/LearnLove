"""可插拔媒体处理层：本地模型或远程 API 都通过同一任务接口接入。"""

from collections.abc import Callable

from agent.archive import finish_media_job, pending_media_jobs
from agent.protocol import err, ok

_providers: dict[str,tuple[str,Callable[[dict],str]]] = {}


def _ensure_local_providers() -> None:
    """懒加载已就绪的默认本地模型，不覆盖显式注册的 API。"""
    from agent.local_media import register_local_media_providers

    register_local_media_providers(set(_providers))


def register_media_provider(capability: str, provider_name: str,
                            handler: Callable[[dict],str]) -> None:
    """注册本地或远程处理器；handler 接收任务与归档路径并返回文本结果。"""
    if capability not in ("speech_to_text","image_understanding"):
        raise ValueError("不支持的媒体能力")
    _providers[capability] = (provider_name,handler)


def media_api_status() -> dict:
    """查看处理器、待处理数量和本地模型运行条件。"""
    from agent.local_media import discover_local_media

    _ensure_local_providers()

    jobs = pending_media_jobs(limit=100)
    counts = {}
    for job in jobs:
        counts[job["capability"]] = counts.get(job["capability"],0)+1
    return ok({"providers":{key:value[0] for key,value in _providers.items()},
               "pending":counts,
               "local":discover_local_media(),
               "ready":{"speech_to_text":"speech_to_text" in _providers,
                        "image_understanding":"image_understanding" in _providers}})


def process_media_queue(capability: str = "", limit: int = 10) -> dict:
    """调用已注册处理器；未配置时只报告，不丢任务。"""
    _ensure_local_providers()
    if capability and capability not in ("speech_to_text","image_understanding"):
        return err("capability 必须是 speech_to_text 或 image_understanding")
    jobs = pending_media_jobs(capability=capability,limit=limit)
    completed,failed,waiting = 0,0,0
    for job in jobs:
        provider = _providers.get(job["capability"])
        if not provider or not job.get("archived_path"):
            waiting += 1
            continue
        name,handler = provider
        try:
            result = handler(job)
            finish_media_job(job["job_id"],name,str(result))
            completed += 1
        except Exception as exc:
            finish_media_job(job["job_id"],name,error=str(exc))
            failed += 1
    return ok({"completed":completed,"failed":failed,"waiting":waiting,
               "note":"未配置的能力会保持待处理，可在接入 API 后补跑"})
