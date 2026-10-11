# Pippit web protocol and acceptance evidence

Captured from the logged-in `pip-1` Chromium session on 2026-10-11. The capture stored endpoint names, field structure and response states only; it did not retain cookies, signatures, prompts, or media bytes. The implementation uses the browser session and the site's direct video tool. It does not call Pippit Skills or use a Pippit upstream API key.

## Request chain

1. The browser's native file input uploads image, video and audio media. The observed chain is `/lv/v1/asset/prepare_upload_cloud`, cloud transfer, `/lv/v1/asset/create_cloud_asset`, and `/api/biz/v3/asset/create_v2`.
   If cloud upload responses arrive but the browser never reaches asset registration, the media upload is retried once with a fresh file input path. This retry occurs before any video submission.
2. `/api/biz/v1/asset/query` confirms the new Pippit asset ID and returns the media record used to build the direct tool references.
3. `/api/biz/v1/agent/submit_run` submits `biz/x_tool_name_video_part` to `pippit_video_part_agent`. The `message.content[].data` value is JSON text containing a `param` value that is also JSON text. The outer `run_extra` value **must be JSON text**, not an object. Sending it as an object was reproducibly rejected with `ret=-3: bad request`; changing only this encoding allowed submission with the previously successful browser media.
4. `/api/biz/v1/agent/get_run` polls with `run_id`, `thread_id` and `scopes:["all"]`. State `3` is complete. The final artifact's `video.download_url` is the preferred result URL.
5. `/commerce/v1/benefits/user_credit` reports account credit buckets. `/commerce/v1/benefits/credit_receive` is the optional daily claim.

The browser's successful direct tool request sent explicit `model`, `duration_sec`, `resolution`, and `ratio` fields. Media mentions in the prompt use `<pippit-asset-id>...</pippit-asset-id>`. Captured requests also carried browser SDK headers such as `x-secsdk-web-signature`, `x-secsdk-web-expire`, and `x-secsdk-sign-config`; PIP2API issues same-origin requests inside the logged-in browser so the site's request handling supplies them. Their values are never stored.

When a fixed ratio and video reference are present, PIP2API adds a short reference-generation instruction to the prompt while keeping the structured parameters unchanged. This addresses the provider's tendency to classify some prompts as video editing, which rejects explicit output ratio and duration. It does not override a provider-side rejection.

## Live acceptance

- Public API task: `pip_67e81942ec654b91`
- Request: `sd-2-0-mini-480p`, 4 seconds, 16:9, one freshly uploaded image, one freshly uploaded 2-second video reference, one freshly uploaded audio reference.
- Result: `succeeded`, one downloadable video with both video and audio streams. `ffprobe` measured 4.067 seconds and an encoded picture size of 864×496. The provider's 480p setting is therefore a quality tier, not a promise of exactly 480 encoded rows.
- Observed credit debit: 30 credits. The service records this observed amount for future matching estimates; it does not infer a fixed price for unobserved model and media combinations.

Only the Mini 480p mixed-media route has this end-to-end public API acceptance. Other advertised model aliases follow captured parameter conventions and need their own upstream acceptance before treating their output quality or credit cost as verified.
