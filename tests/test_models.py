from datetime import datetime, timezone

from fb_automation.models import IdentityContext, PostJob, PostResult, QuarantineRecord


def test_post_job_roundtrip_with_scheduled_at():
    scheduled = datetime(2026, 6, 4, 12, 30, tzinfo=timezone.utc)
    job = PostJob(
        account_id='acct-1',
        page_id='123',
        caption='valid caption',
        post_type='text',
        scheduled_at=scheduled,
    )
    loaded = PostJob.from_dict(job.to_dict())
    assert loaded == job


def test_post_job_rejects_invalid_type():
    try:
        PostJob(account_id='acct-1', page_id='123', caption='valid caption', post_type='link')
    except ValueError as exc:
        assert 'post_type' in str(exc)
    else:
        raise AssertionError('expected post_type validation error')


def test_identity_from_dict_normalizes_json_lists():
    ctx = IdentityContext.from_dict({
        'account_id': 'acct-1',
        'proxy_url': 'http://proxy-1',
        'viewport': [1280, 720],
        'screen_resolution': [1920, 1080],
        'geolocation': [30.0, 31.0],
    })
    assert ctx.viewport == (1280, 720)
    assert ctx.screen_resolution == (1920, 1080)
    assert ctx.geolocation == {'latitude': 30.0, 'longitude': 31.0}


def test_result_and_quarantine_record_roundtrip():
    result = PostResult(success=False, status='PRIVATE_HTTP_DISABLED', page_id='123')
    assert PostResult.from_dict(result.to_dict()) == result

    record = QuarantineRecord(
        account_id='acct-1',
        level='SOFT',
        reason='test',
        expires_at=None,
        created_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
    )
    assert QuarantineRecord.from_dict(record.to_dict()) == record
