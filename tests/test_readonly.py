import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import create_app
from browser_login import BrowserLogin
from core import (LocalStore, ReadOnlyTransport, ReadOnlySchool, ToolError,
                  allowed_read, browser_allowed, normalize_location, offline_test, online_test,
                  ORIGIN, now)

BASE = 'http://127.0.0.1:8090'
SNAPSHOT = {'student_id': '202300000001', 'saved_at': 123,
            'user_agent': 'Synthetic Test Browser', 'cookies': [
                {'name': 'SESSION', 'value': 'synthetic-cookie-secret', 'domain': 'jzsz.uestc.edu.cn',
                 'path': '/', 'expires': -1, 'secure': True, 'httpOnly': True}]}
POINT = {'name': '测试位置', 'latitude': 30.7528, 'longitude': 103.9313, 'coordinate_system': 'wgs84'}


class GuardTests(unittest.TestCase):
    def test_today_uses_existing_authorization_and_reenters_only_when_expired(self):
        from unittest.mock import MagicMock
        from core import read_today, AuthRequired
        school = MagicMock()
        school.today.return_value = {'recordStatusCode': 'PENDING'}
        self.assertEqual(read_today(school), school.today.return_value)
        school.enter.assert_not_called()
        school.today.side_effect = AuthRequired('session_rejected')
        read_today(school)
        school.enter.assert_called_once()

    def test_every_business_write_is_denied_before_network(self):
        transport = ReadOnlyTransport(SNAPSHOT)
        with patch('core.Transport.request', side_effect=AssertionError('network must not run')):
            for method in ('POST', 'PATCH', 'PUT', 'DELETE', 'GET'):
                for path in ('/student-checkin-record/checkin', '/student-checkin-record/report', '/unknown'):
                    with self.subTest(method=method, path=path), self.assertRaises(ToolError) as error:
                        transport.request(method, ORIGIN+'/daily-checkin/api'+path, stage='test')
                    self.assertEqual(error.exception.reason, 'write_blocked')

    def test_get_with_body_is_denied(self):
        with self.assertRaises(ToolError):
            ReadOnlyTransport(SNAPSHOT).request('GET', ORIGIN+'/', payload={}, stage='test')

    def test_direct_submit_is_disabled(self):
        with self.assertRaises(ToolError):
            ReadOnlySchool(None, {}, None).submit({})

    def test_only_https_school_allowlist_can_be_read(self):
        self.assertTrue(allowed_read('GET', ORIGIN+'/daily-checkin/api/student-checkin-record/today'))
        for url in ('http://jzsz.uestc.edu.cn/', 'https://evil.example/', ORIGIN+':8443/',
                    'https://user:pass@jzsz.uestc.edu.cn/', ORIGIN+'/anything'):
            self.assertFalse(allowed_read('GET', url))

    def test_browser_allows_authentication_and_blocks_attendance(self):
        self.assertTrue(browser_allowed('POST', 'https://idas.uestc.edu.cn/authserver/login'))
        self.assertTrue(browser_allowed('POST', 'https://idas.uestc.edu.cn/authserver/reAuthCheck/verify.do'))
        self.assertTrue(browser_allowed('GET', ORIGIN+'/daily-checkin/checkLogin?appId=294'))
        for method in ('GET', 'POST', 'PUT'):
            self.assertFalse(browser_allowed(method, ORIGIN+'/daily-checkin/api/student-checkin-record/checkin'))
        self.assertFalse(browser_allowed('POST', 'https://evil.example/upload'))

    def test_browser_allows_app_login_initialization_reads_only(self):
        for path in ('/daily-checkin/api/auth/user-info',
                     '/daily-checkin/api/user/service-account-subscribed'):
            with self.subTest(path=path):
                self.assertTrue(browser_allowed('GET', ORIGIN+path))
                self.assertTrue(browser_allowed('HEAD', ORIGIN+path))
                self.assertEqual(allowed_read('GET', ORIGIN+path), path.endswith('/auth/user-info'))
                self.assertFalse(browser_allowed('POST', ORIGIN+path))
        self.assertFalse(browser_allowed('GET', ORIGIN+'/daily-checkin/api/auth/unknown'))

    def test_browser_allows_official_wechat_qr_and_assets_without_other_access(self):
        urls = (
            'https://open.weixin.qq.com/connect/qrconnect?appid=synthetic',
            'https://open.weixin.qq.com/connect/qrcode/synthetic',
            'https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid=synthetic',
            'https://long.open.weixin.qq.com/connect/l/qrconnect?uuid=synthetic',
            'https://res.wx.qq.com/t/wx_fed/mp/connect/res/static/css/login.css',
            'https://res.wx.qq.com/t/wx_fed/weui-source/res/2.6.21/weui.min.css',
            'https://res.wx.qq.com/t/wx_fed/cdn_libs/res/jquery/1.11.3/jquery.min.js',
            'https://res.wx.qq.com/a/wx_fed/assets/res/favicon.ico',
            'https://res.wx.qq.com/t/fed_upload/avatar.svg',
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(browser_allowed('GET', url))
                self.assertFalse(browser_allowed('POST', url))
                self.assertFalse(allowed_read('GET', url))
        for url in ('http://open.weixin.qq.com/connect/qrconnect',
                    'https://open.weixin.qq.com/unknown',
                    'https://open.weixin.qq.com:8443/connect/qrconnect',
                    'https://user:password@open.weixin.qq.com/connect/qrconnect',
                    'https://lp.open.weixin.qq.com/unknown',
                    'https://res.wx.qq.com/unknown',
                    'https://open.weixin.qq.com.evil.example/connect/qrconnect'):
            self.assertFalse(browser_allowed('GET', url))

    def test_offline_never_calls_network(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network')):
            result = offline_test(POINT)
        self.assertTrue(result['passed'])
        self.assertEqual(result['schoolRequests'], 0)
        self.assertEqual(len(result['blockedRequests']), 3)

    def test_coordinate_conversion_and_rejection(self):
        value = normalize_location(POINT)
        self.assertNotAlmostEqual(value['latitude'], value['gcj02']['latitude'], places=5)
        gcj = normalize_location({**POINT, 'coordinate_system': 'gcj02'})
        self.assertEqual(gcj['latitude'], gcj['gcj02']['latitude'])
        for bad in (float('nan'), float('inf'), 91):
            with self.assertRaises(ToolError):
                normalize_location({**POINT, 'latitude': bad})

    def test_online_uses_get_only_and_returns_no_raw_personal_record(self):
        from moonuestc.transport import Response
        calls = []
        def respond(transport, method, url, *, stage, **kwargs):
            self.assertTrue(allowed_read(method, url))
            calls.append((method, url))
            if stage == 'portal_warmup':
                body = {'code': 0}
            elif stage == 'micro_app_entry':
                return Response(200, {}, b'', ORIGIN+'/daily-checkin-h5/index.html?appId=294')
            elif stage == 'today':
                body = {'code': 0, 'data': {'businessDate': now().date().isoformat(),
                    'recordStatusCode': 'PENDING', 'abnormalFlag': False, 'name': 'private-person'}}
            elif stage == 'schedule':
                body = {'code': 0, 'data': {'effective': True,
                    'studentCheckinStartOffsetMinute': 1260, 'studentCheckinEndOffsetMinute': 1410}}
            elif stage == 'locations':
                body = {'code': 0, 'data': [{**normalize_location(POINT)['gcj02'], 'radiusMeter': 200}]}
            else:
                raise AssertionError(stage)
            return Response(200, {}, json.dumps(body).encode(), url)
        with patch('core.Transport.request', new=respond):
            result = online_test(SNAPSHOT, POINT)
        self.assertEqual(len(calls), 3)
        self.assertTrue(result['insideFence'])
        self.assertEqual(result['schoolWrites'], 0)
        self.assertNotIn('private-person', json.dumps(result))


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.client = self.app.test_client()
        self.status = self.client.get('/api/status', base_url=BASE).json
        self.headers = {'Origin': BASE, 'X-CSRF-Token': self.status['csrf']}

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, value):
        return self.client.post(path, json=value, base_url=BASE, headers=self.headers)

    def test_cookie_is_encrypted_and_never_in_public_status(self):
        store = self.app.extensions['local_store']
        store.save_session(SNAPSHOT)
        blob = (Path(self.temp.name)/'session.enc').read_bytes()
        self.assertNotIn(b'synthetic-cookie-secret', blob)
        self.assertEqual(store.session(), SNAPSHOT)
        state = self.client.get('/api/status', base_url=BASE).json
        self.assertEqual(state['account']['cookie_count'], 1)
        self.assertNotIn('synthetic-cookie-secret', json.dumps(state))

    def test_tampered_cookie_rejected(self):
        store = self.app.extensions['local_store']
        store.save_session(SNAPSHOT)
        path = Path(self.temp.name)/'session.enc'
        path.write_bytes(path.read_bytes()[:-1]+b'x')
        with self.assertRaises(ToolError):
            store.session()

    def test_cross_origin_and_missing_csrf_denied(self):
        self.assertEqual(self.client.post('/api/test/offline', json={}, base_url=BASE).status_code, 403)
        self.assertEqual(self.client.post('/api/test/offline', json={}, base_url=BASE,
                                         headers={**self.headers, 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', base_url='http://evil.example').status_code, 403)

    def test_save_location_and_offline_check(self):
        self.assertEqual(self.post('/api/location', POINT).status_code, 200)
        result = self.post('/api/test/offline', {}).json['result']
        self.assertTrue(result['passed'])
        self.assertFalse(result['submitted'])

    def test_online_requires_imported_session(self):
        self.assertEqual(self.post('/api/test/online', {}).status_code, 400)

    def test_submit_requires_explicit_confirmation(self):
        self.assertEqual(self.post('/api/checkin', {}).status_code, 400)
        self.assertTrue(self.status['schoolWritesEnabled'])

    def test_forget_removes_local_session(self):
        store = self.app.extensions['local_store']
        store.save_session(SNAPSHOT)
        self.assertEqual(self.post('/api/session/forget', {}).status_code, 200)
        self.assertIsNone(store.session())


class LoginTests(unittest.TestCase):
    def test_both_browser_choices_use_isolated_profiles(self):
        for browser, channel in (('chrome','chrome'),('edge','msedge')):
            with self.subTest(browser=browser), tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
                store=LocalStore(directory); manager=BrowserLogin(store)
                launch=playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                context=launch.return_value
                context.cookies.return_value=SNAPSHOT['cookies']
                page=context.new_page.return_value
                page.url=ORIGIN+'/'
                page.is_closed.return_value=False
                page.evaluate.return_value=SNAPSHOT['user_agent']
                with patch('browser_login.verify_session',return_value={'checkin_mode':'individual','can_self_checkin':True}):
                    manager.run(SNAPSHOT['student_id'],'synthetic',browser=browser)
                self.assertEqual(manager.status()['state'],'success')
                self.assertEqual(launch.call_args.kwargs['channel'],channel)
                self.assertTrue(Path(launch.call_args.args[0]).is_relative_to(Path(directory)/'browser-temp'))
                context.add_cookies.assert_not_called()

    def test_identity_read_verifies_login_without_attendance_permission(self):
        from core import verify_session
        from moonuestc.transport import Response
        user = {'role':'001', 'studentIdentityTypes':['GROUP_MEMBER'], 'id':'synthetic-private-id'}
        with patch('core.Transport.request', return_value=Response(200, {},
                json.dumps({'code':0,'data':{'userData':user}}).encode(),
                ORIGIN+'/daily-checkin/api/auth/user-info')) as send:
            identity = verify_session(SNAPSHOT)
        self.assertEqual(identity, {'checkin_mode':'group_member', 'can_self_checkin':False})
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], ORIGIN+'/daily-checkin/api/auth/user-info')
        self.assertNotIn('synthetic-private-id', str(identity))

    def test_identity_read_recognizes_personal_checkin_and_rejects_invalid_user(self):
        from core import verify_session
        from moonuestc.transport import Response
        for user, expected in [({'role':'001','studentIdentityTypes':[]}, 'individual'),
                               ({'role':'001','studentIdentityTypes':['CLASS_CADRE','GROUP_MEMBER']}, 'group_filling'),
                               ({'role':'001','studentIdentityTypes':['SAFETY_OFFICER']}, 'fill_out_today')]:
            with self.subTest(user=user), patch('core.Transport.request', return_value=Response(200, {},
                    json.dumps({'code':0,'data':{'userData':user}}).encode(), ORIGIN+'/')):
                self.assertEqual(verify_session(SNAPSHOT)['checkin_mode'], expected)
        with patch('core.Transport.request', return_value=Response(200, {}, b'{"code":0,"data":{}}', ORIGIN+'/')):
            with self.assertRaises(ToolError) as error:
                verify_session(SNAPSHOT)
        self.assertEqual(error.exception.reason, 'invalid_user_info')

    def test_login_selects_password_tab_when_school_defaults_to_wechat(self):
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            manager = BrowserLogin(LocalStore(directory))
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.cookies.return_value = []
            page = context.new_page.return_value
            page.url = 'https://idas.uestc.edu.cn/authserver/login'
            page.is_closed.return_value = True
            from unittest.mock import MagicMock
            password = MagicMock(); password.count.return_value = 0
            account_tab = MagicMock()
            inputs = {}
            def locator(selector):
                if selector == 'input#password:visible':
                    return password
                if selector == '#userNameLogin_a:visible':
                    return account_tab
                return inputs.setdefault(selector, MagicMock())
            page.locator.side_effect = locator
            manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            account_tab.first.click.assert_called_once()
            inputs['input#username:visible'].first.fill.assert_called_once_with(SNAPSHOT['student_id'], timeout=10000)
            password.first.fill.assert_called_once_with('synthetic-password', timeout=5000)

    def test_browser_session_is_saved_only_after_verified_read(self):
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            from unittest.mock import MagicMock
            store = LocalStore(directory)
            manager = BrowserLogin(store)
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.cookies.return_value = SNAPSHOT['cookies']
            page = context.new_page.return_value
            page.url = 'https://jzsz.uestc.edu.cn/'
            page.is_closed.return_value = False
            page.evaluate.return_value = SNAPSHOT['user_agent']
            with patch('browser_login.verify_session', return_value=None) as verify:
                manager.run(SNAPSHOT['student_id'], 'synthetic-password-not-to-save')
            verify.assert_called_once()
            from browser_login import ENTRY_URL
            self.assertIn(ENTRY_URL, context.cookies.call_args.args[0])
            self.assertEqual(manager.status()['state'], 'success')
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            self.assertEqual(launch.call_args.kwargs['channel'], 'chrome')
            self.assertEqual(store.session()['cookies'], SNAPSHOT['cookies'])
            self.assertNotIn('password', store.session())
            context.close.assert_called_once()

    def test_browser_uses_private_profile_and_artifacts_without_changing_process_temp(self):
        import os
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            store = LocalStore(directory); manager = BrowserLogin(store)
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.side_effect = PermissionError('sensitive-path-or-url-must-not-be-returned')
            original = (os.environ.get('TEMP'), os.environ.get('TMP'))
            manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            call = launch.call_args
            profile = Path(call.args[0]); artifacts = Path(call.kwargs['artifacts_dir'])
            self.assertEqual(profile.parent, artifacts.parent)
            self.assertTrue(profile.is_relative_to(Path(directory)))
            self.assertEqual(call.kwargs['env']['TEMP'], str(profile.parent))
            self.assertEqual(call.kwargs['env']['TMP'], str(profile.parent))
            self.assertEqual(original, (os.environ.get('TEMP'), os.environ.get('TMP')))
            self.assertEqual(manager.status()['reason'], 'browser_temp_permission')
            self.assertNotIn('sensitive', str(manager.status()))
            self.assertIsNone(store.session())

    def test_missing_chrome_has_actionable_error_without_falling_back_to_edge(self):
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright:
            store = LocalStore(directory)
            manager = BrowserLogin(store)
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.side_effect = RuntimeError('Chrome executable not found: sensitive-local-path')
            with patch('browser_login.NETWORK.browser_options', return_value={}):
                manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            launch.assert_called_once()
            self.assertEqual(launch.call_args.kwargs['channel'], 'chrome')
            self.assertEqual(manager.status()['reason'], 'chrome_not_installed')
            self.assertIn('Chrome', manager.status()['message'])
            self.assertNotIn('sensitive-local-path', str(manager.status()))
            self.assertIsNone(store.session())

    def test_login_retains_app_scoped_cookies(self):
        from browser_login import ENTRY_URL
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            store = LocalStore(directory)
            manager = BrowserLogin(store)
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            app_cookie = dict(SNAPSHOT['cookies'][0], path='/daily-checkin', value='synthetic-app-cookie')
            context.cookies.side_effect = lambda urls: SNAPSHOT['cookies'] + ([app_cookie] if ENTRY_URL in urls else [])
            page = context.new_page.return_value
            page.url = 'https://jzsz.uestc.edu.cn/'
            page.is_closed.return_value = False
            page.evaluate.return_value = SNAPSHOT['user_agent']
            with patch('browser_login.verify_session', return_value=None) as verify:
                manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            self.assertIn(app_cookie, verify.call_args.args[0]['cookies'])
            self.assertIn(app_cookie, store.session()['cookies'])

    def test_verification_log_has_codes_without_private_message(self):
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            manager = BrowserLogin(LocalStore(directory))
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.cookies.return_value = SNAPSHOT['cookies']
            page = context.new_page.return_value
            page.url = 'https://jzsz.uestc.edu.cn/'
            page.is_closed.return_value = False
            page.evaluate.return_value = SNAPSHOT['user_agent']
            page.wait_for_timeout.side_effect = lambda _: manager.cancel()
            failure = ToolError('business_error', 'today', 4003, http_status=200,
                                school_message='synthetic-private-message')
            with self.assertLogs('browser_login', level='WARNING') as logs, patch('browser_login.verify_session', side_effect=failure):
                manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            text = '\n'.join(logs.output)
            self.assertIn('stage=today reason=business_error http_status=200 api_code=4003', text)
            self.assertNotIn('synthetic-private-message', text)
            self.assertNotIn('synthetic-cookie-secret', text)

    def test_cancel_during_verification_does_not_save_session(self):
        with tempfile.TemporaryDirectory() as directory, patch('playwright.sync_api.sync_playwright') as playwright, patch('browser_login.NETWORK.browser_options', return_value={}):
            store = LocalStore(directory)
            manager = BrowserLogin(store)
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.cookies.return_value = SNAPSHOT['cookies']
            context.new_page.return_value.is_closed.return_value = False
            context.new_page.return_value.url = 'https://jzsz.uestc.edu.cn/'
            context.new_page.return_value.evaluate.return_value = SNAPSHOT['user_agent']
            with patch('browser_login.verify_session', side_effect=lambda *args: manager.cancel()):
                manager.run(SNAPSHOT['student_id'], 'synthetic-password')
            self.assertEqual(manager.status()['state'], 'cancelled')
            self.assertIsNone(store.session())


class ProgressTests(unittest.TestCase):
    def test_progress_finishes_even_after_error(self):
        from progress import Progress
        progress = Progress()
        with self.assertRaises(ValueError):
            with progress.operation('prepare'):
                progress.step('正在查询地址')
                self.assertTrue(progress.snapshot()['active'])
                self.assertEqual(progress.snapshot()['message'], '正在查询地址')
                raise ValueError('synthetic error')
        self.assertFalse(progress.snapshot()['active'])


if __name__ == '__main__':
    unittest.main()
