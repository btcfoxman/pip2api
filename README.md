# PIP2API

PIP2API exposes Pippit's logged-in web video workflow as a task API. The management console follows the AK2API account pool and task layout. Upstream video requests use the web `agent/submit_run` direct video tool. Media are uploaded through the same logged-in Chromium session, including its native file upload control. The separate Pippit Skills API and API keys are not used.

## Deployment

Copy `.env.example` to `.env` and set unique `PIP_API_KEY` and `PIP_ADMIN_TOKEN` values. Start with `docker compose up -d`. The console is on port `8807` by default; `/health` is unauthenticated. Use **在线添加账号** with a name and account proxy, then log in through the embedded noVNC browser and click **完成登录并验证**. Verification opens an empty Canvas project automatically when the account is still on the Pippit home page; it does not submit a paid task. The account row's **网页登录** action reconnects to the same Chrome. The verified browser stays running and its CDP session is used by task submission, polling, credit checks, and daily check-in. After a service restart, the browser is restored from its persisted profile.

During online login verification, PIP2API reads the account's Pippit watermark setting and enables the no-watermark option if the plan allows it. It reads the setting again before enabling the account. If the plan requires a watermark, login still completes and the console reports that limitation.

The preproduction workflow is `.github/workflows/deploy-test.yml`. It builds an image in GHCR and deploys on the `pip2api-pre` self-hosted runner on 3.5 pre. Prepare `/home/btcfoxman/docker/pip2api/.env` on the server and register the runner before dispatch. The workflow also manages the `pip2api.aiid.edu.kg` cloudflared ingress.

Set a SOCKS or HTTP proxy on each account before opening its managed browser. Pippit login, credit, upload, submit, and polling requests all use that account's proxied Chromium session. The managed browser refuses to start without an account proxy. For `pip-1.bat`, the account's server-side proxy is `socks5://host.docker.internal:20001`, corresponding to the launcher's `socks5://127.0.0.1:20001` on the host.

## Video API

All API endpoints require `Authorization: Bearer <PIP_API_KEY>` or `X-API-Key`.

```http
POST /v1/videos
Content-Type: application/json
```

```json
{
  "model": "sd-2-0-mini-480p",
  "prompt": "A blue bird sits on a branch beside image1. Use audio1 and video1 as references.",
  "duration": 4,
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/image.png"],
  "audio_urls": ["https://example.com/audio.wav"],
  "video_urls": ["https://example.com/reference.mp4"],
  "background": true
}
```

The response contains a `pip_...` task ID. Poll `GET /v1/videos/{task_id}` for `queued`, `preparing`, `submitted`, `running`, `succeeded`, `failed`, or `expired`. Successful video URLs appear in `data[].url`; `GET /v1/videos/{task_id}/content` redirects to the first video. A caller may also supply `pippit:<asset-id>` for an asset already uploaded to that same account. Plain image, video and audio URLs, or base64 data URLs, are supported.

Prompt references such as `视频1`, `video2`, `v3`, `视4`, `图片1`, `图1`, `img2`, `Image3`, `Audio1`, and `音频2` are bound by their one-based index to the corresponding uploaded asset. Out-of-range references fail before upstream submission. Seedance 2.0 models accept up to 9 images, 3 videos, and 3 audio clips; Seedance 2.5 models accept up to 30, 10, and 10 respectively. The requested duration, ratio, resolution and model are sent as direct tool parameters.

`GET /v1/models` lists the 11 public aliases and limits. Aliases without a resolution suffix default to 720p. The console shows observed credit usage by exact model, duration, resolution, and media counts. These observations guide account selection; no unsupported fixed credit formula is assumed.

## Operations

- Account concurrency defaults to 1 and can be changed to at most 50. Global workers and queue capacity are configured in the console. Queue overflow returns HTTP 429 before upstream submission.
- A complete upstream rejection is recorded as `UPSTREAM_REJECTED`. An interrupted submit with unknown outcome is recorded as `UPSTREAM_SUBMIT_OUTCOME_UNKNOWN`; it is never automatically resubmitted.
- The daily credit claim is optional. Enable it in runtime settings or click the account check-in endpoint. Each account is claimed at most once per Taiwan calendar day.
- Media download first uses a direct connection, then the account or fallback proxy. Browser upload uses the logged-in Chromium tab. The service does not need a Pippit Skills key.
- The online login window is available only to an authenticated console session. Its noVNC stream stays on the same origin and supports clipboard paste. Passwords typed there are not stored by PIP2API. Chrome profile data remain under `PIP_CHROME_USER_DATA_ROOT` and are removed when a service-managed account profile is reset or deleted.

The upstream web protocol can change. If Pippit changes its direct tool or browser upload flow, an upstream rejection is surfaced in the task record rather than silently changing the requested generation parameters.
