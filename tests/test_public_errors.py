from app.errors import public_error


def test_historical_pippit_failures_are_classified_for_callers():
    cases = (
        ("MEDIAERROR", "media download failed through account proxy: SSLError",
         "MEDIA_DOWNLOAD_FAILED"),
        ("MEDIAERROR", "Pippit web upload failed: Pippit asset creation did not complete",
         "MEDIA_UPLOAD_FAILED"),
        ("UPSTREAM_FAILED", "{'code': 12011, 'message': 'The content did not meet platform requirements.'}",
         "CONTENT_MODERATION_FAILED"),
        ("UPSTREAM_FAILED", "{'code': 99999, 'message': 'The model identified this as a video editing task.'}",
         "VIDEO_EDIT_CONSTRAINT"),
        ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "browser disconnected",
         "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"),
        ("UPSTREAM_FAILED", "agent callback execute failed: submit GenerateVideo failed",
         "UPSTREAM_TEMPORARY"),
    )
    for old_code, upstream_message, expected_code in cases:
        normalized = public_error(old_code, upstream_message)
        assert normalized["code"] == expected_code
        assert upstream_message not in normalized["message"]
