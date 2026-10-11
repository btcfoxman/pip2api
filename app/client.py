"""Pippit Web direct-video RPC built from captured browser requests."""

from __future__ import annotations

import json
import ast
import hashlib
import os
import threading
import time
import uuid
from typing import Any

from .browser import CDPBrowser, account_proxy_url
from .catalog import bind_prompt
from .media import _download, upload_media, upload_media_bytes
from .store import Store


_MEDIA_LOCKS_GUARD = threading.RLock()
_MEDIA_LOCKS: dict[int, threading.RLock] = {}


class PippitError(Exception):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _asset_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items()
            if key not in {"mime", "bytes"}}


def build_video_param(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ids = {kind: [str(item["pippit_asset_id"]) for item in assets[kind]]
           for kind in ("images", "videos", "audios")}
    prompt = bind_prompt(normalized["prompt"], ids)
    if ids["videos"] and normalized["aspect_ratio"] != "adaptive":
        # The agent may otherwise interpret a reference clip as a video-editing
        # request, for which Pippit rejects an explicitly requested ratio and
        # duration. Keep the caller's structured settings authoritative.
        prompt += ("\n\n参考视频仅作画面与动作参考；这是生成一条全新视频的任务，"
                   "不要剪辑或修改参考视频，也不要沿用参考视频的时长或画幅。"
                   f"最终只生成一条视频，模型 {normalized['upstream_model']}，"
                   f"时长 {normalized['duration']} 秒，分辨率 {normalized['resolution']}，"
                   f"画幅 {normalized['aspect_ratio']}。")
    result: dict[str, Any] = {
        "prompt": prompt,
        "images": [_asset_ref(item) for item in assets["images"]],
        "videos": [_asset_ref(item) for item in assets["videos"]],
        "audios": [_asset_ref(item) for item in assets["audios"]],
        "duration_sec": normalized["duration"],
        "ratio": normalized["aspect_ratio"],
        "resolution": normalized["resolution"],
        "model": normalized["upstream_model"],
        "language": "zh",
    }
    # The direct web tool sends these optional fields explicitly as null.
    result.update({key: None for key in (
        "imitation_videos", "extend_type", "characters", "generate_type", "seed",
        "mini_tool_param", "node_asset_refs", "style_snapshot",
        "partial_edit_videos", "storyboard_param", "task_type",
        "file_pippit_asset_ids", "links", "sug_id",
    )})
    return result


def build_submit_body(param: dict[str, Any], project_id: str,
                      user_info: dict[str, str]) -> dict[str, Any]:
    thread_id, run_id, message_id = _uuid(), _uuid(), _uuid()
    message = {
        "content": [{
            "type": "data", "sub_type": "biz/x_data_direct_tool_call_req",
            "data": json.dumps({"tool_name": "biz/x_tool_name_video_part",
                                "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
                                "extra": "{}"},
                               ensure_ascii=False, separators=(",", ":")),
        }],
        "role": "user", "thread_id": thread_id, "run_id": run_id,
        "message_id": message_id, "created_at": int(time.time() * 1000),
    }
    return {
        "project_id": int(project_id), "message": message, "user_info": user_info,
        "agent_name": "pippit_video_part_agent",
        "concurrency_control": {"policy": 1, "conflict_key": run_id},
        "entrance_from": "web_video_direct",
        "run_extra": json.dumps({"babi_param": {"edit_type": "integrated_agent",
            "generate_id": run_id, "user_message_id": message_id, "section_id": run_id,
            "scene_lv1": "ai_agent", "scene_lv2": "front_tool",
            "tool_id": "integrated_agent_canvas", "tab_name": "canvas",
            "enter_from": "home_page_integrated_agent_combo_input_box"},
            "client_extra": {"current_page": "/home", "entrance_from":
                             "home_page_integrated_agent_combo_input_box",
                             "generate_mode": "video_gen", "generate_position": "home",
                             "canvas_type": "integrated_agent", "edit_type": "integrated_agent",
                             "project_id": int(project_id), "position": "canvas",
                             "tab_name": "canvas", "target": "video",
                             "run_source": "video_part", "placement_name": "canvas"}},
            ensure_ascii=False, separators=(",", ":")),
        "request_id": _uuid(),
    }


def _walk_urls(value: Any, result: list[str], parent_key: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and item.startswith("https://") and (
                key.lower() in {"url", "video_url", "download_url", "preview_url", "result_url"}
                and ("video" in parent_key.lower() or key.lower() != "url")
            ):
                if item not in result:
                    result.append(item)
            else:
                _walk_urls(item, result, key)
    elif isinstance(value, list):
        for item in value:
            _walk_urls(item, result, parent_key)
    elif isinstance(value, str) and value.startswith(("{", "[")):
        try:
            _walk_urls(json.loads(value), result, parent_key)
        except ValueError:
            pass


def extract_result_urls(run: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for entry in run.get("entry_list") or []:
        if not isinstance(entry, dict):
            continue
        artifact = entry.get("artifact") or {}
        for content in artifact.get("content") or []:
            data = content.get("data") if isinstance(content, dict) else None
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = None
            if isinstance(data, dict):
                video = data.get("video") or {}
                if isinstance(video, dict):
                    url = video.get("download_url") or video.get("url")
                    if isinstance(url, str) and url.startswith("https://") and url not in result:
                        result.append(url)
        if result:
            continue
        for container in (entry.get("message"), entry.get("artifact")):
            if container:
                _walk_urls(container, result)
    return result


def extract_failure(run: dict[str, Any]) -> tuple[str, str]:
    reason = run.get("fail_reason")
    if isinstance(reason, str) and reason.startswith("{"):
        for decode in (json.loads, ast.literal_eval):
            try:
                reason = decode(reason)
                break
            except (ValueError, SyntaxError):
                continue
    if isinstance(reason, dict):
        code = str(reason.get("code") or "")
        message = str(reason.get("detail") or reason.get("message") or "")
        return code, message[:500]
    return "", str(reason or "")[:500]


class PippitClient:
    def __init__(self, account: dict[str, Any], *, store: Store | None = None):
        account_proxy_url(account)
        self.account = account
        self.store = store
        self.browser = CDPBrowser(int(account["cdp_port"]))

    def account_state(self) -> dict[str, Any]:
        data = self.browser.post("/commerce/v1/benefits/user_credit", {})
        credit = data.get("credit") or {}
        balance = sum(float(credit.get(key) or 0) for key in
                      ("vip_credit", "gift_credit", "purchase_credit"))
        return {"available_balance": balance, "buckets": credit}

    def daily_checkin(self) -> dict[str, Any]:
        return self.browser.post("/commerce/v1/benefits/credit_receive",
                                 {"source": "web", "time_zone": os.getenv("PIP_TIME_ZONE", "Asia/Taipei")})

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        proxy = str(self.account.get("proxy_url") or "")
        project_id = str(self.account.get("project_id") or self.browser.project_id())
        result: dict[str, list[dict[str, Any]]] = {}
        for kind in ("images", "videos", "audios"):
            result[kind] = []
            for source in normalized[kind]:
                if not self.store or source.startswith("pippit:"):
                    uploaded = upload_media(source, kind, browser=self.browser, proxy_url=proxy)
                    result[kind].append(self._query_asset_ref(uploaded, kind, project_id))
                    continue
                data, mime, filename = _download(source, kind, proxy)
                digest = hashlib.sha256(data).hexdigest()
                account_id = int(self.account["id"])
                with _MEDIA_LOCKS_GUARD:
                    lock = _MEDIA_LOCKS.setdefault(account_id, threading.RLock())
                with lock:
                    cached = self.store.media_cache_get(account_id, kind, digest)
                    if cached:
                        try:
                            result[kind].append(self._query_asset_ref(cached, kind, project_id))
                            continue
                        except PippitError:
                            self.store.media_cache_delete(account_id, kind, digest)
                    uploaded = upload_media_bytes(data, mime, filename, kind,
                                                  browser=self.browser)
                    reference = self._query_asset_ref(uploaded, kind, project_id)
                    self.store.media_cache_put(account_id, kind, digest, uploaded)
                    result[kind].append(reference)
        return result

    def _query_asset_ref(self, uploaded: dict[str, Any], kind: str,
                         project_id: str) -> dict[str, Any]:
        asset_id = str(uploaded["pippit_asset_id"])
        data = self.browser.post("/api/biz/v1/asset/query", {
            "Base": {"Client": "web"}, "project_id": int(project_id),
            "pippit_asset_ids": [asset_id],
        })
        matches = [record for record in data.get("Assets") or []
                   if str(record.get("PippitAssetID")) == asset_id]
        if not matches:
            raise PippitError("Pippit uploaded asset is not available for submission")
        record = matches[0]
        base = {"asset_id": str(record.get("SourceID") or uploaded.get("asset_id") or ""),
                "pippit_asset_id": asset_id}
        if kind == "images":
            image = record.get("Image") or {}
            metadata = dict(image.get("metadata") or {})
            metadata.setdefault("mime", uploaded.get("mime") or "image/png")
            metadata.setdefault("name", record.get("Name") or "image")
            metadata.setdefault("frame_cnt", 1)
            return {**base, "metadata": metadata, "url": None, "caption": None,
                    "uri": None, "scene_urls": None, "description": None,
                    "name": record.get("Name"), "security_check_scene": None,
                    "securityCheckScene": None}
        if kind == "videos":
            video = record.get("Video") or {}
            metadata = dict(video.get("metadata") or {})
            metadata.setdefault("mime", uploaded.get("mime") or "video/mp4")
            metadata.setdefault("name", record.get("Name") or "video")
            return {**base, "metadata": metadata, "url": None,
                    "description": None, "frames": None,
                    "cover_preview_url": None, "cover_preview_key": None,
                    "player_info": None, "security_check_scene": None,
                    "share_url": None, "time_range": None,
                    "securityCheckScene": None}
        audio = record.get("Audio") or {}
        return {**base, "metadata": None, "url": audio.get("download_url"),
                "description": None, "frames": None, "cover_preview_url": None,
                "cover_preview_key": None, "player_info": None,
                "securityCheckScene": None, "share_url": None,
                "time_range": None, "security_check_scene": None}

    def submit(self, normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        project_id = str(self.account.get("project_id") or self.browser.project_id())
        if not project_id.isdigit():
            raise PippitError("open a Pippit video project before submitting")
        param = build_video_param(normalized, assets)
        body = build_submit_body(param, project_id, self.browser.user_info())
        response = self.browser.post("/api/biz/v1/agent/submit_run", body)
        run = response.get("run") or {}
        if not run.get("run_id") or not run.get("thread_id"):
            raise PippitError("Pippit submit response has no run ID or thread ID")
        return {"run_id": str(run["run_id"]), "thread_id": str(run["thread_id"]),
                "state": run.get("state"), "param": param}

    def poll(self, run_id: str, thread_id: str) -> dict[str, Any]:
        data = self.browser.post("/api/biz/v1/agent/get_run",
                                 {"scopes": ["all"], "run_id": run_id, "thread_id": thread_id})
        run = data.get("run") or {}
        if not run:
            raise PippitError("Pippit get_run response has no run")
        state = str(run.get("state") or "")
        status = {"3": "succeeded", "4": "failed", "5": "failed", "9": "failed"}.get(state, "running")
        error_code, error = extract_failure(run)
        return {"status": status, "state": state,
                "result_urls": extract_result_urls(run),
                "error_code": error_code, "error": error}
