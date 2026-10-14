"""Pippit account settings must be verified before an online login is enabled."""

import unittest
from unittest.mock import Mock

from app.client import PippitClient, PippitError


class WatermarkSettingTests(unittest.TestCase):
    def client(self, responses):
        client = PippitClient({'cdp_port': 19302,
                               'proxy_url': 'socks5://proxy.test:20001'})
        client.browser = Mock()
        client.browser.user_info.return_value = {'consumer_uid': '42'}
        client.browser.post.side_effect = responses
        return client

    @staticmethod
    def setting(enabled, forced=False):
        return {'42': {'aigc_watermark_ban_enabled': enabled,
                       'must_pippit_ai_watermark_enabled': forced}}

    def test_already_enabled_does_not_write(self):
        client = self.client([self.setting(True)])
        self.assertEqual(client.ensure_no_watermark(), 'enabled')
        client.browser.post.assert_called_once_with(
            '/api/biz/v1/user/mget_user_setting', {})

    def test_disabled_is_enabled_and_read_back(self):
        client = self.client([self.setting(False), {}, self.setting(True)])
        self.assertEqual(client.ensure_no_watermark(), 'enabled')
        self.assertEqual(client.browser.post.call_args_list[1].args, (
            '/api/biz/v1/user/update_user_setting',
            {'user_settings': {'aigc_watermark_ban_enabled': True}},
        ))
        self.assertEqual(client.browser.post.call_count, 3)

    def test_plan_forced_watermark_is_not_toggled(self):
        client = self.client([self.setting(False, forced=True)])
        self.assertEqual(client.ensure_no_watermark(), 'plan_requires_watermark')
        self.assertEqual(client.browser.post.call_count, 1)

    def test_unconfirmed_update_fails_login_verification(self):
        client = self.client([self.setting(False), {}, self.setting(False)])
        with self.assertRaises(PippitError):
            client.ensure_no_watermark()


if __name__ == '__main__':
    unittest.main()
