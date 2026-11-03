"""Transform client-owned history without pretending to recover remote state."""

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class CleaningReport:
    reasoning_items: int = 0
    item_ids: int = 0
    response_links: int = 0


def clean_request(request, *, allow_response_link_removal=False):
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象。")
    if request.get("conversation"):
        raise ValueError("无法恢复远端 conversation 历史，请新开会话。")
    linked = bool(request.get("previous_response_id"))
    if linked and not allow_response_link_removal:
        raise ValueError("请求依赖 previous_response_id。只有确认客户端发送完整历史后，才可启用高级选项。")
    result = deepcopy(request)
    result.pop("previous_response_id", None)
    history = result.get("input")
    removed_reasoning = removed_ids = 0
    if isinstance(history, list):
        retained = []
        for entry in history:
            if not isinstance(entry, dict):
                raise ValueError("input 数组中的每项都必须是对象。")
            category = entry.get("type")
            if category in {"compaction", "item_reference"}:
                raise ValueError("历史包含加密压缩项或远端引用，无法清洗。请新开会话。")
            if category == "reasoning":
                removed_reasoning += 1
                continue
            if "id" in entry:
                del entry["id"]
                removed_ids += 1
            retained.append(entry)
        result["input"] = retained
    elif history is not None and not isinstance(history, str):
        raise ValueError("input 必须是字符串或数组。")
    return result, CleaningReport(removed_reasoning, removed_ids, int(linked))
