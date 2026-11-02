"""Daily claims are idle-only, identity-bound and independent of gift usage."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import json

from app.browser import BrowserError, BrowserRejected
from app.client import PippitClient, PippitError, validate_checkin_result
from app.daily_checkin import AccountCheckinBusy
from app.service import Service
from app.store import Store


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'daily.db'))
    service = Service(store)
    account = store.upsert_account({'name': 'daily', 'cdp_port': 19301,
        'proxy_url': 'socks5://fixture.test:20001'})
    store.update_account(account['id'], {'enabled': True, 'status': 'active', 'balance': 10,
        'balance_details': {'vip_credit': 10, 'gift_credit': 0, 'purchase_credit': 0}}, login_identity='42')
    account = store.account(account['id'], private=True)
    monkeypatch.setattr(service.browser_runtime, 'acquire', Mock(side_effect=lambda row, **_: (row, False)))
    monkeypatch.setattr(service.browser_runtime, 'release', Mock())
    monkeypatch.setattr(service.browser_runtime, 'close_idle', Mock())
    monkeypatch.setattr(service._checkin_pool, 'submit', Mock())
    client = Mock()
    client.login_identity.return_value = '42'
    client.daily_checkin.return_value = {'receive_quota': 0}
    client.account_state.return_value = {'available_balance': 100,
        'buckets': {'vip_credit': 10, 'gift_credit': 90, 'purchase_credit': 0}}
    monkeypatch.setattr('app.daily_checkin.PippitClient', Mock(return_value=client))
    day = {'value': '2026-11-02'}
    monkeypatch.setattr('app.daily_checkin.checkin_day', lambda: day['value'])
    yield SimpleNamespace(store=store, service=service, account=account, client=client, day=day)
    service.stop()
    assert not service._checkin_owners and not service._health_inflight


def receipt(context):
    return context.store.account(context.account['id'], private=True)


def run_scheduled(context):
    call = context.service._checkin_pool.submit.call_args
    function, *args = call.args
    function(*args)


def test_busy_claim_drains_existing_work_and_last_release_wakes_without_claiming_early(context):
    service, identifier = context.service, context.account['id']
    service.update_settings({'daily_checkin_enabled': True})
    service._active[identifier] = 1
    service._schedule_daily_checkins()
    assert identifier in service._checkin_draining
    service._checkin_pool.submit.assert_not_called()
    assert service._reserve_account() is None
    context.client.daily_checkin.assert_not_called()
    service._checkin_wakeup.clear()
    service._release_account(identifier)
    assert service._checkin_wakeup.is_set()
    service._schedule_daily_checkins()
    def claim():
        assert not service._active.get(identifier, 0)
        assert service._reserve_account() is None
        assert context.client.account['login_user_id'] == '42'
        assert context.client.browser.timeout == 20
        return {'receive_quota': 0}
    context.client.daily_checkin.side_effect = claim
    run_scheduled(context)
    assert receipt(context)['last_checkin_date'] == context.day['value']
    assert receipt(context)['balance'] == 100
    context.client.daily_checkin.assert_called_once()
    assert not service._checkin_draining and not service._checkin_pending


def test_only_two_accounts_drain_while_the_other_five_continue_accepting_tasks(context):
    service, store = context.service, context.store
    for index in range(2, 8):
        account = store.upsert_account({'name': f'account-{index}', 'cdp_port': 19300 + index,
            'proxy_url': 'socks5://fixture.test:20001'})
        store.update_account(account['id'], {'status': 'active', 'balance': 10}, login_identity=str(index))
    service.update_settings({'daily_checkin_enabled': True})
    service._schedule_daily_checkins()
    assert len(service._checkin_pending) == 7 and len(service._checkin_draining) == 2
    assert service._checkin_pool.submit.call_count == 2
    selected = service._reserve_account()
    assert selected and selected['id'] not in service._checkin_draining
    service._release_account(selected['id'])


def test_gift_spent_today_is_not_a_missing_claim_and_day_rollover_is_eligible(context):
    context.service.checkin_account(context.account['id'])
    context.store.update_account(context.account['id'], {'balance': 0,
        'balance_details': {'vip_credit': 0, 'gift_credit': 0, 'purchase_credit': 0}})
    assert context.service.checkin_account(context.account['id'])['already_checked_in'] is True
    context.client.daily_checkin.assert_called_once()
    context.service.update_settings({'daily_checkin_enabled': True})
    context.service._schedule_daily_checkins()
    context.service._checkin_pool.submit.assert_not_called()
    context.day['value'] = '2026-11-03'
    context.service._schedule_daily_checkins()
    run_scheduled(context)
    assert context.client.daily_checkin.call_count == 2
    assert receipt(context)['last_checkin_date'] == '2026-11-03'


def test_concurrent_claims_send_one_rpc_and_repeated_today_call_is_local(context):
    entered, release = Event(), Event()
    def claim():
        entered.set()
        assert release.wait(1)
        return {'receive_quota': 0}
    context.client.daily_checkin.side_effect = claim
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(context.service.checkin_account, context.account['id'])
        assert entered.wait(1)
        with pytest.raises(AccountCheckinBusy):
            context.service.checkin_account(context.account['id'])
        release.set()
        assert first.result(timeout=1)['balance_refreshed'] is True
    assert context.service.checkin_account(context.account['id'])['already_checked_in'] is True
    context.client.daily_checkin.assert_called_once()


def test_claim_rejection_does_not_mark_date_or_balance_and_security_rejection_backs_off(context):
    context.service.update_settings({'daily_checkin_enabled': True})
    context.client.daily_checkin.side_effect = BrowserRejected('Pippit ret=34070104: shark action check reject')
    context.service._schedule_daily_checkins()
    run_scheduled(context)
    account = receipt(context)
    assert account['last_checkin_date'] == '' and account['balance'] == 10
    assert account['enabled'] and account['status'] == 'active'
    assert context.service._checkin_retry[account['id']][2] > 0
    context.service._schedule_daily_checkins()
    assert context.service._checkin_pool.submit.call_count == 1
    assert not context.service._checkin_draining
    context.client.account_state.assert_not_called()


def test_identity_changes_during_postclaim_credit_read_preserve_confirmed_receipt_and_cached_balance(context):
    context.client.login_identity.side_effect = ['42', '42', '42', 'different']
    result = context.service.checkin_account(context.account['id'])
    account = receipt(context)
    assert result['balance_refreshed'] is False
    assert account['last_checkin_date'] == context.day['value']
    assert account['last_checkin_user_id'] == '42' and account['balance'] == 10
    assert json.loads(account['balance_details_json'])['_stale'] is True
    context.client.daily_checkin.assert_called_once()


@pytest.mark.parametrize('value', [{}, [], {'receive_quota': True}, {'receive_quota': -1},
                                   {'receive_quota': float('nan')}, {'already_received': False}])
def test_unconfirmed_payload_never_marks_claim_success(context, value):
    context.client.daily_checkin.return_value = value
    with pytest.raises(PippitError):
        context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_date'] == '' and receipt(context)['balance'] == 10
    context.client.account_state.assert_not_called()


def test_confirmed_claim_survives_balance_refresh_failure_and_restart(context):
    context.client.account_state.side_effect = BrowserError('synthetic refresh unavailable')
    result = context.service.checkin_account(context.account['id'])
    assert result['balance_refreshed'] is False
    assert receipt(context)['last_checkin_date'] == context.day['value']
    assert receipt(context)['last_checkin_user_id'] == '42' and receipt(context)['balance'] == 10
    assert json.loads(receipt(context)['balance_details_json'])['_stale'] is True
    reopened = Store(str(context.store.path))
    assert reopened.account(context.account['id'], private=True)['last_checkin_user_id'] == '42'
    assert context.service.checkin_account(context.account['id'])['already_checked_in'] is True
    context.client.daily_checkin.assert_called_once()


def test_legacy_date_is_rebound_from_current_profile_without_forcing_login(context):
    context.store.update_account(context.account['id'], {'cdp_port': 0})
    context.store.update_account(context.account['id'], {'last_checkin_date': context.day['value']})
    assert receipt(context)['login_user_id'] == receipt(context)['last_checkin_user_id'] == ''
    context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_user_id'] == '42'
    assert context.service.browser_runtime.acquire.call_count == 1


def test_private_identity_is_not_exposed_and_profile_change_invalidates_daily_receipt(context):
    context.service.checkin_account(context.account['id'])
    public = context.store.account(context.account['id'])
    assert 'login_user_id' not in public and 'last_checkin_user_id' not in public
    context.store.update_account(context.account['id'], {'status': 'profile_resetting'})
    assert receipt(context)['last_checkin_date'] == ''
    assert receipt(context)['login_user_id'] == receipt(context)['last_checkin_user_id'] == ''


def test_health_check_and_claim_cannot_overwrite_each_others_credit_snapshot(context):
    context.service._health_inflight.add(context.account['id'])
    with pytest.raises(AccountCheckinBusy):
        context.service.checkin_account(context.account['id'])
    context.service._health_inflight.clear()
    context.client.daily_checkin.assert_not_called()
    context.service._checkin_owners.add(context.account['id'])
    with pytest.raises(AccountCheckinBusy):
        context.service.check_account(context.account['id'])
    context.service._checkin_owners.clear()


def test_disabling_automatic_claims_after_queueing_releases_drain_without_a_claim(context):
    context.service.update_settings({'daily_checkin_enabled': True})
    context.service._schedule_daily_checkins()
    context.service.update_settings({'daily_checkin_enabled': False})
    run_scheduled(context)
    assert not context.service._checkin_draining
    context.client.daily_checkin.assert_not_called()


def test_identity_mismatch_or_late_profile_change_cannot_claim_or_mark_another_user(context):
    context.client.login_identity.return_value = 'different-user'
    with pytest.raises(BrowserError, match='identity does not match'):
        context.service.checkin_account(context.account['id'])
    context.client.daily_checkin.assert_not_called()
    context.client.login_identity.return_value = '42'
    def switched():
        context.store.update_account(context.account['id'], {'proxy_url': 'socks5://changed.test:20001'})
        return {'receive_quota': 90}
    context.client.daily_checkin.side_effect = switched
    with pytest.raises(ValueError, match='identity changed'):
        context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_date'] == '' and receipt(context)['login_user_id'] == ''
    assert receipt(context)['balance'] == 10


def test_auto_respects_flags_and_manual_endpoint_keeps_explicit_override(context):
    context.store.update_account(context.account['id'], {'enabled': False})
    context.service.update_settings({'daily_checkin_enabled': True})
    context.service._schedule_daily_checkins()
    context.service._checkin_pool.submit.assert_not_called()
    context.service.update_settings({'daily_checkin_enabled': False})
    assert not context.service._checkin_pending
    context.service.checkin_account(context.account['id'])
    context.client.daily_checkin.assert_called_once()
    assert not receipt(context)['enabled']


def test_health_account_exception_does_not_kill_the_remaining_maintenance_pass(context, monkeypatch):
    second = context.store.upsert_account({'name': 'second', 'cdp_port': 19302,
        'proxy_url': 'socks5://fixture.test:20001'})
    context.store.update_account(second['id'], {'status': 'active'})
    check = Mock(side_effect=[PippitError('synthetic non-BrowserError'), {'status': 'active'}])
    monkeypatch.setattr(context.service, 'check_account', check)
    assert context.service._maintain_accounts_once() is True
    assert check.call_count == 2


def test_client_ret_zero_payload_validation_accepts_legitimate_zero_and_explicit_already(monkeypatch):
    client = PippitClient({'cdp_port': 19301, 'proxy_url': 'socks5://fixture.test:20001'})
    browser = Mock()
    client.browser = browser
    browser.user_info.return_value = {'consumer_uid': '42'}
    browser.daily_checkin_from_page.return_value = {'receive_quota': 0}
    monkeypatch.setenv('PIP_TIME_ZONE', 'Asia/Taipei')
    assert client.daily_checkin() == {'receive_quota': 0}
    browser.daily_checkin_from_page.assert_called_once_with('42', 'Asia/Taipei')
    browser.post.assert_not_called()
    validate_checkin_result({'already_received': True})
    browser.daily_checkin_from_page.return_value = {}
    with pytest.raises(PippitError): client.daily_checkin()


def test_cold_identity_wait_reads_page_only_and_never_repeats_a_claim(monkeypatch):
    client = PippitClient({'cdp_port': 19301, 'proxy_url': 'socks5://fixture.test:20001'})
    client.browser = Mock()
    client.browser.user_info.side_effect = [BrowserError('page not ready'), {'consumer_uid': '42'}]
    monkeypatch.setattr('app.client.time.sleep', Mock())
    assert client.login_identity() == '42'
    assert client.browser.user_info.call_count == 2
    client.browser.post.assert_not_called()


def test_request_crossing_midnight_retains_request_day_then_allows_new_day(context):
    def claim():
        context.day['value'] = '2026-11-03'
        return {'receive_quota': 0}
    context.client.daily_checkin.side_effect = claim
    context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_date'] == '2026-11-02'
    context.client.daily_checkin.side_effect = None
    context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_date'] == '2026-11-03'
    assert context.client.daily_checkin.call_count == 2


def test_late_health_snapshot_cannot_overwrite_a_newly_verified_profile(context, monkeypatch):
    client = Mock()
    def switched():
        context.store.update_account(context.account['id'], {'balance': 777}, login_identity='new-user')
        return {'available_balance': 100, 'buckets': {'vip_credit': 0, 'gift_credit': 100, 'purchase_credit': 0}}
    client.account_state.side_effect = switched
    client.browser.project_id.return_value = ''
    monkeypatch.setattr('app.service.PippitClient', Mock(return_value=client))
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    with pytest.raises(ValueError, match='identity changed'):
        context.service.check_account(context.account['id'])
    assert receipt(context)['balance'] == 777 and receipt(context)['login_user_id'] == 'new-user'


def test_existing_database_keeps_legacy_day_while_new_identity_fields_start_unbound(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            cdp_port INTEGER NOT NULL DEFAULT 0,proxy_url TEXT NOT NULL DEFAULT '',project_id TEXT NOT NULL DEFAULT '',
            max_concurrency INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'pending',
            balance REAL,balance_details_json TEXT NOT NULL DEFAULT '{}',last_checkin_date TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)""")
        db.execute("INSERT INTO accounts(id,name,last_checkin_date,updated_at) VALUES(1,'legacy','2026-11-02',1)")
    store = Store(str(path))
    account = store.account(1, private=True)
    assert account['last_checkin_date'] == '2026-11-02'
    assert account['login_user_id'] == account['last_checkin_user_id'] == ''


def test_identity_switch_after_receipt_is_not_marked_confirmed(context):
    context.client.login_identity.side_effect = ['42', 'different-user']
    with pytest.raises(BrowserError, match='changed before its receipt'):
        context.service.checkin_account(context.account['id'])
    assert receipt(context)['last_checkin_date'] == ''
    assert receipt(context)['balance'] == 10
    context.client.account_state.assert_not_called()


def test_normal_health_refresh_clears_stale_details_without_another_claim(context, monkeypatch):
    context.client.account_state.side_effect = BrowserError('refresh pending')
    context.service.checkin_account(context.account['id'])
    assert json.loads(receipt(context)['balance_details_json'])['_stale'] is True
    health = Mock()
    health.account_state.return_value = {'available_balance': 84,
        'buckets': {'vip_credit': 4, 'gift_credit': 80, 'purchase_credit': 0}}
    health.browser.project_id.return_value = ''
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    monkeypatch.setattr('app.service.PippitClient', Mock(return_value=health))
    context.service.check_account(context.account['id'])
    assert json.loads(receipt(context)['balance_details_json'])['_stale'] is False
    assert receipt(context)['balance'] == 84
    context.service.checkin_account(context.account['id'])
    context.client.daily_checkin.assert_called_once()
