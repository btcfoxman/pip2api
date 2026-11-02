"""Bounded, idle-only daily claims, independent of slow account health checks."""
from __future__ import annotations

import logging
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .browser import BrowserError, BrowserRejected
from .client import PippitClient, validate_checkin_result

logger = logging.getLogger(__name__)


class AccountCheckinBusy(BrowserError):
    """Existing account work finishes before its daily claim may start."""


def checkin_day() -> str:
    return datetime.now(ZoneInfo(os.getenv('PIP_TIME_ZONE', 'Asia/Taipei'))).date().isoformat()


def confirmed_today(account: dict, day: str) -> bool:
    identity = account.get('login_user_id')
    return bool(identity and account.get('last_checkin_date') == day
                and account.get('last_checkin_user_id') == identity)


def identity_snapshot(account: dict) -> dict:
    return {key: account.get(key) for key in ('cdp_port', 'proxy_url', 'managed_profile', 'login_user_id')}


def validated_credit_state(state: dict) -> tuple[float, dict]:
    balance, buckets = state.get('available_balance'), state.get('buckets')
    if (isinstance(balance, bool) or not isinstance(balance, (int, float))
            or not math.isfinite(balance) or balance < 0 or not isinstance(buckets, dict)):
        raise ValueError('Pippit authoritative credit balance is invalid')
    for key in ('vip_credit', 'gift_credit', 'purchase_credit'):
        value = buckets.get(key)
        if isinstance(value, bool) or value is None or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError('Pippit authoritative credit bucket is invalid')
    return balance, {**buckets, '_stale': False, '_observed_at': time.time()}


class DailyCheckinMixin:
    def _init_daily_checkins(self) -> None:
        self._checkin_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pip-checkin')
        self._checkin_wakeup = threading.Event()
        self._checkin_thread = None
        self._checkin_pending = {}
        self._checkin_draining = set()
        self._checkin_inflight = set()
        self._checkin_owners = set()
        self._checkin_locks = {}
        self._checkin_retry = {}

    def _start_daily_checkins(self) -> None:
        self._checkin_thread = threading.Thread(target=self._daily_checkin_loop,
            name='pip-daily-checkin', daemon=True)
        self._checkin_thread.start()

    def _stop_daily_checkins(self) -> None:
        self._checkin_wakeup.set()
        if self._checkin_thread and self._checkin_thread.is_alive():
            self._checkin_thread.join(timeout=5)
        self._checkin_pool.shutdown(wait=False, cancel_futures=True)

    def _daily_checkin_loop(self) -> None:
        while not self._stop.is_set():
            self._checkin_wakeup.clear()
            try:
                self._schedule_daily_checkins()
            except Exception:
                logger.exception('PIP daily check-in dispatcher failed; retrying')
            self._checkin_wakeup.wait(30)

    def _schedule_daily_checkins(self) -> None:
        if self._stop.is_set():
            return
        day, now = checkin_day(), time.monotonic()
        with self._lock:
            if not self.settings()['daily_checkin_enabled']:
                self._checkin_pending.clear()
                self._checkin_draining.clear()
                return
            eligible = set()
            for account in self.store.accounts(private=True):
                identifier = int(account['id'])
                if not account['enabled'] or account['status'] != 'active' or not account['proxy_url']:
                    continue
                if confirmed_today(account, day):
                    self._checkin_retry.pop(identifier, None)
                    continue
                retry_day, _, due = self._checkin_retry.get(identifier, (day, 0, 0))
                if retry_day == day and now < due:
                    continue
                eligible.add(identifier)
                self._checkin_pending[identifier] = day
            for identifier in list(self._checkin_pending):
                if identifier not in eligible and identifier not in self._checkin_inflight:
                    self._checkin_pending.pop(identifier, None)
                    self._checkin_draining.discard(identifier)
            # Drain at most two accounts. The rest of the pool keeps accepting
            # generation work while these accounts finish their existing tasks.
            for identifier in self._checkin_pending:
                if len(self._checkin_draining) >= 2:
                    break
                self._checkin_draining.add(identifier)
            for identifier in list(self._checkin_pending):
                if len(self._checkin_inflight) >= 2:
                    break
                if (identifier not in self._checkin_draining
                        or identifier in self._checkin_inflight or identifier in self._checkin_owners
                        or identifier in self._health_inflight
                        or self._active.get(identifier, 0) or self.store.active_account_count(identifier)):
                    continue
                self._checkin_inflight.add(identifier)
                try:
                    self._checkin_pool.submit(self._run_daily_checkin, identifier, day)
                except RuntimeError:
                    self._checkin_inflight.discard(identifier)
                    if not self._stop.is_set():
                        raise

    def _run_daily_checkin(self, identifier: int, day: str) -> None:
        busy = False
        try:
            self.checkin_account(identifier, automatic=True)
            with self._lock:
                self._checkin_retry.pop(identifier, None)
        except AccountCheckinBusy:
            busy = True
        except Exception as exc:
            with self._lock:
                old_day, count, _ = self._checkin_retry.get(identifier, (day, 0, 0))
                count = count + 1 if old_day == day else 1
                delay = min(1800, 300 * 2 ** min(count - 1, 3)) if isinstance(exc, BrowserRejected) else min(300, 30 * 2 ** min(count - 1, 4))
                self._checkin_retry[identifier] = (day, count, time.monotonic() + delay)
            logger.warning('PIP daily check-in deferred account_id=%s error_type=%s: %.200s',
                           identifier, type(exc).__name__, str(exc))
        finally:
            with self._lock:
                self._checkin_inflight.discard(identifier)
                if not busy:
                    self._checkin_pending.pop(identifier, None)
                    self._checkin_draining.discard(identifier)
            self._checkin_wakeup.set()

    def checkin_account(self, account_id: int, *, automatic: bool = False) -> dict:
        with self._lock:
            lock = self._checkin_locks.setdefault(account_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise AccountCheckinBusy('Account daily check-in is already in progress')
        account, owned, claimed_owner = {}, False, False
        try:
            with self._lock:
                account = self.store.account(account_id, private=True)
                if not account:
                    raise KeyError(account_id)
                day = checkin_day()
                if confirmed_today(account, day):
                    return {'already_checked_in': True, 'date': day}
                if self._stop.is_set() or automatic and (not self.settings()['daily_checkin_enabled'] or not account['enabled']
                                  or account['status'] != 'active'):
                    return {'skipped': True, 'date': day}
                if (account['status'] in {'logging_in', 'profile_resetting'} or self._active.get(account_id, 0)
                        or account_id in self._health_inflight or self.store.active_account_count(account_id)):
                    raise AccountCheckinBusy('Account tasks must finish before daily check-in')
                self._checkin_owners.add(account_id)
                claimed_owner = True
            account, owned = self.browser_runtime.acquire(account)
            client = PippitClient(account)
            # Leave CDP time to receive the XHR's bounded timeout result.
            client.browser.timeout = 20
            identity = client.login_identity()
            expected = str(account.get('login_user_id') or '')
            if expected and identity != expected:
                raise BrowserError('Pippit daily check-in account identity does not match the saved profile')
            with self._lock:
                self.store.update_account(account_id, {}, login_identity=identity,
                                          expected=identity_snapshot(account))
                account = self.store.account(account_id, private=True)
                client.account = account
                if confirmed_today(account, day):
                    return {'already_checked_in': True, 'date': day}
                if self._stop.is_set() or automatic and (not self.settings()['daily_checkin_enabled'] or not account['enabled']):
                    return {'skipped': True, 'date': day}
            # The date and API request use the same configured timezone. Retain
            # the request day if a response crosses midnight; next day's claim
            # is a new daily operation, never a duplicate inferred from gift=0.
            day = checkin_day()
            result = client.daily_checkin()
            validate_checkin_result(result)
            if client.login_identity() != identity:
                raise BrowserError('Pippit daily check-in account changed before its receipt was verified')
            previous_buckets = json.loads(account.get('balance_details_json') or '{}')
            self.store.update_account(account_id, {'last_checkin_date': day,
                'balance_details': {**previous_buckets, '_stale': True, '_stale_since': time.time()}}, checkin_identity=identity,
                                      expected=identity_snapshot(account))
            refreshed = False
            try:
                if client.login_identity() != identity:
                    raise BrowserError('Pippit daily check-in identity changed before balance refresh')
                state = client.account_state()
                if client.login_identity() != identity:
                    raise BrowserError('Pippit daily check-in identity changed during balance refresh')
                balance, buckets = validated_credit_state(state)
                self.store.update_account(account_id, {'balance': balance, 'balance_details': buckets},
                                          expected=identity_snapshot(account))
                refreshed = True
            except Exception as exc:
                logger.warning('PIP daily check-in confirmed; balance refresh deferred account_id=%s error_type=%s',
                               account_id, type(exc).__name__)
            return {'already_checked_in': bool(result.get('already_received') or result.get('already_checked_in')),
                    'date': day, 'received': result.get('receive_quota'), 'balance_refreshed': refreshed}
        finally:
            try:
                if account:
                    self.browser_runtime.release(account, owned)
                    if owned:
                        try:
                            self.browser_runtime.close_idle(account_id)
                        except Exception:
                            logger.warning('PIP daily check-in browser cleanup deferred account_id=%s', account_id)
            finally:
                with self._lock:
                    if claimed_owner:
                        self._checkin_owners.discard(account_id)
                lock.release()
                self._checkin_wakeup.set()
