import json

import pytest

from app.catalog import bind_prompt, normalize_request, public_models
from app.client import build_submit_body, build_video_param, extract_failure, extract_result_urls


def test_requested_aliases_and_media_limits():
    names = {model["id"] for model in public_models()}
    assert names == {
        "sd-2-0", "sd-2-0-1080p", "sd-2-0-4k", "sd-2-0-fast",
        "sd-2-0-mini-lite", "sd-2-0-mini", "sd-2-0-mini-480p",
        "sd-2-0-fast-480p", "sd-2-5", "sd-2-5-480p", "sd-2-5-1080p",
    }
    with pytest.raises(ValueError, match="at most 9 images"):
        normalize_request({"model": "sd-2-0-mini", "prompt": "bird", "image_urls": ["pippit:x"] * 10})
    assert len(normalize_request({"model": "sd-2-5", "prompt": "bird",
                                  "image_urls": ["pippit:x"] * 30})["images"]) == 30


def test_prompt_references_bind_to_correct_media():
    request = normalize_request({"model": "sd-2-0-mini-480p", "duration": 4,
        "prompt": "video1 then v1, 图片1, img1, 音频1 and Audio1",
        "image_urls": ["pippit:image"], "video_urls": ["pippit:video"],
        "audio_urls": ["pippit:audio"]})
    assert request["resolution"] == "480p"
    prompt = bind_prompt(request["prompt"], {"images": ["image"],
        "videos": ["video"], "audios": ["audio"]})
    assert prompt.count("<pippit-asset-id>video</pippit-asset-id>") == 2
    assert "<pippit-asset-id>image</pippit-asset-id>" in prompt
    assert "<pippit-asset-id>audio</pippit-asset-id>" in prompt
    with pytest.raises(ValueError, match="only 1 images"):
        normalize_request({"prompt": "图2", "image_urls": ["pippit:image"]})


def test_direct_tool_submit_envelope_and_final_video_selection():
    request = normalize_request({"model": "sd-2-0-mini-480p", "duration": 4,
        "prompt": "A blue bird", "image_urls": ["pippit:image"],
        "video_urls": ["pippit:video"], "audio_urls": ["pippit:audio"]})
    assets = {kind: [{"pippit_asset_id": kind + "-asset"}]
              for kind in ("images", "videos", "audios")}
    param = build_video_param(request, assets)
    body = build_submit_body(param, "12345", {"consumer_uid": "u",
        "workspace_id": "w", "space_id": "s", "app_id": "573081"})
    tool = json.loads(body["message"]["content"][0]["data"])
    assert body["agent_name"] == "pippit_video_part_agent"
    assert isinstance(body["run_extra"], str)
    assert json.loads(body["run_extra"])["babi_param"]["tool_id"] == "integrated_agent_canvas"
    assert tool["tool_name"] == "biz/x_tool_name_video_part"
    assert json.loads(tool["param"])["model"] == "Seedance_2.0_mini"
    assert [len(param[kind]) for kind in ("images", "videos", "audios")] == [1, 1, 1]
    assert "4" in param["prompt"] and "480p" in param["prompt"]
    assert "16:9" in param["prompt"]
    run = {"entry_list": [{"artifact": {"content": [{"data": json.dumps({
        "video": {"url": "https://cdn.example/video", "preview_url": "https://cdn.example/preview",
                  "download_url": "https://cdn.example/download"}})}]}}]}
    assert extract_result_urls(run) == ["https://cdn.example/download"]


def test_reference_video_constraints_do_not_change_structured_parameters():
    request = normalize_request({"model": "sd-2-5-480p", "duration": 4,
        "aspect_ratio": "16:9", "prompt": "video1 as motion reference",
        "video_urls": ["pippit:clip"]})
    assets = {"images": [], "videos": [{"pippit_asset_id": "clip"}], "audios": []}
    param = build_video_param(request, assets)
    assert param["duration_sec"] == 4
    assert param["ratio"] == "16:9"
    assert param["model"] == "Seedance_2.5"
    assert param["prompt"].startswith("<pippit-asset-id>clip</pippit-asset-id>")
    assert "480p" in param["prompt"]
    request["aspect_ratio"] = "adaptive"
    assert build_video_param(request, assets)["prompt"] == param["prompt"].split("\n\n", 1)[0]


def test_upstream_failure_details_are_extracted_from_structured_reason():
    code, message = extract_failure({"fail_reason": {"code": 99999,
        "message": "generic", "detail": "The model identified this as a video editing task."}})
    assert code == "99999"
    assert message == "The model identified this as a video editing task."
