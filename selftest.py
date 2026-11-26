"""Packaged runtime test: synthetic encryption, diagnostics, and a blank browser."""
import tempfile
from core import LocalStore, offline_test


def run(browser='chrome'):
    from browser_login import browser_choice
    channel, label = browser_choice(browser)
    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(directory)
        sample = {'cookies': [], 'user_agent': 'Synthetic Runtime Test', 'student_id': '000000000000'}
        store.save_session(sample)
        assert store.session() == sample
        assert offline_test(None)['schoolRequests'] == 0
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True)
        try:
            assert browser.new_page().evaluate('1 + 1') == 2
        finally:
            browser.close()
    print(f'Runtime self-test PASS: encryption, offline guards, {label}; schoolRequests=0')
