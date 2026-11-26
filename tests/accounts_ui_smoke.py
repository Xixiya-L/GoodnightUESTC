"""Exercise browser selection and account saves with mocked school services."""
import sys, tempfile, threading, time
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
from waitress import create_server
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import create_app

def fake_login(self, student_id, password, browser='chrome'):
    self.browser=browser
    self.store.save_session({'student_id':student_id,'saved_at':time.time(),'user_agent':'Synthetic','cookies':[]})
    self.state={'state':'success','message':'模拟登录已完成'}

def fake_wechat(self, snapshot):
    value={'student_id':snapshot['student_id'],'last_verified_at':time.time(),'credentials':{'access_token':'synthetic','expires_at':2000000000}}
    (self.store.directory/'wechat.enc').write_bytes(self.store.vault.seal(value,'wechat-authorization'))
    self.state={'state':'success','message':'模拟微信取票成功，没有提交'}

def main():
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as p, patch('browser_login.BrowserLogin.start',autospec=True,side_effect=fake_login) as login, patch('wechat_auth.WeChatAuthorization.start',fake_wechat), patch('wechat_auth.WeChatAuthorization.ready',return_value=True):
        app=create_app(directory)
        server=create_server(app,host='127.0.0.1',port=8092,threads=4)
        worker=threading.Thread(target=server.run,daemon=True);worker.start()
        browser=p.chromium.launch(channel='chrome',headless=True)
        errors=[]
        try:
            page=browser.new_page(viewport={'width':1200,'height':1100})
            page.on('pageerror',lambda error:errors.append(str(error)))
            def proxy(route):
                response=route.fetch(url=route.request.url.replace(':8090',':8092'),headers={**route.request.headers,'host':'127.0.0.1:8090'})
                route.fulfill(response=response)
            page.route('http://127.0.0.1:8090/**',proxy)
            page.goto('http://127.0.0.1:8090/',wait_until='domcontentloaded')
            expect(page.locator('#saved-account')).to_be_visible()
            profiles=[]
            for index,choice in enumerate(['chrome','edge']):
                page.locator('#new-account').click()
                expect(page.locator('#library-message')).to_contain_text('正在配置新草稿')
                identifier=page.url.split('account=')[1]
                profiles.append(identifier)
                page.locator('#account-name').fill('测试'+str(index+1))
                page.locator('#login-browser').select_option(choice)
                label='Chrome' if choice=='chrome' else 'Edge'
                expect(page.locator('#login-button')).to_contain_text(label)
                expect(page.locator('#browser-help')).to_contain_text(label)
                page.locator('#student-id').fill('20230000000'+str(index+1))
                page.locator('#password').fill('SyntheticOnly')
                page.locator('#login-button').click()
                expect(page.locator('#session-badge')).to_have_text('已保存')
                assert login.call_args.kwargs['browser']==choice
                page.locator('#save-account').click()
                expect(page.locator('#library-message')).to_contain_text('已保存学校登录')
                page.locator('#saved-account option:checked').filter(has_text='已保存登录').wait_for(state='attached')

            # Finish the second account without refreshing its library summary.
            # Saving must use the current authorization state.
            page.locator('#wechat-start').click()
            expect(page.locator('#wechat-message')).to_have_text('模拟微信取票成功，没有提交')
            page.locator('#map').click(position={'x':220,'y':150})
            page.locator('#place-name').fill('测试位置')
            page.locator('#save-location').click()
            expect(page.locator('#converted')).to_contain_text('GCJ-02：')
            expect(page.locator('#save-account')).to_have_text('保存 / 更新到账号库')
            page.locator('#save-account').click()
            expect(page.locator('#library-message')).to_contain_text('正在使用已存账号')
            records=app.test_client().get('/api/accounts',base_url='http://127.0.0.1:8090').json['profiles']
            assert records[0]['login_saved'] and not records[0]['ready']
            assert records[1]['ready']
            for index,identifier in enumerate(profiles):
                page.locator('#saved-account').select_option(identifier)
                expect(page.locator('#student-id')).to_have_value('20230000000'+str(index+1))
            assert not errors,errors
            print('Account UI PASS: Chrome/Edge selection, login saves, full save after authorization, account switching')
        finally:
            browser.close();server.close();worker.join(timeout=3)
if __name__=='__main__':main()
