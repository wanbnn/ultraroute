import json
from pathlib import Path

import pytest

from ultraroute.errors import GatewayError
from ultraroute.store import Store
from ultraroute.system_browser import SystemBrowser, SystemBrowserLauncher


@pytest.fixture(autouse=True)
def isolated_system_browser_paths(monkeypatch):
    monkeypatch.setattr('ultraroute.system_browser.windows_browser_paths', lambda: [])
    monkeypatch.setattr('ultraroute.system_browser.user_data_root', lambda: Path.home() / '.local/share/ultraroute')


@pytest.mark.asyncio
async def test_flatpak_detection_and_no_duplicate_install(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('ultraroute.system_browser.shutil.which', lambda name: '/usr/bin/flatpak' if name == 'flatpak' else None)
    async def command(command, timeout=15):
        calls.append(command)
        return 0, 'org.chromium.Chromium\n', ''
    monkeypatch.setattr('ultraroute.system_browser.command_output', command)
    launcher = SystemBrowserLauncher(Store(tmp_path))
    browser = await launcher.ensure_browser()
    assert browser.id == 'flatpak:org.chromium.Chromium'
    assert browser.command == ('/usr/bin/flatpak', 'run', 'org.chromium.Chromium')
    assert not any('install' in c for c in calls)


@pytest.mark.asyncio
async def test_auto_install_only_when_missing_and_user_scoped(tmp_path, monkeypatch):
    installed, calls = False, []
    monkeypatch.setattr('ultraroute.system_browser.shutil.which', lambda name: '/usr/bin/flatpak' if name == 'flatpak' else None)
    async def command(command, timeout=15):
        nonlocal installed
        calls.append(command)
        if 'install' in command:
            installed = True
        return 0, 'org.chromium.Chromium\n' if installed and 'list' in command else '', ''
    monkeypatch.setattr('ultraroute.system_browser.command_output', command)
    launcher = SystemBrowserLauncher(Store(tmp_path))
    assert (await launcher.ensure_browser()).kind == 'flatpak'
    install = next(c for c in calls if 'install' in c)
    assert '--user' in install and '--noninteractive' in install
    assert launcher.install_state == 'installed'


@pytest.mark.asyncio
async def test_snap_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr('ultraroute.system_browser.shutil.which', lambda name: '/usr/bin/snap' if name == 'snap' else None)
    async def command(command, timeout=15):
        return 0, 'chromium installed', ''
    monkeypatch.setattr('ultraroute.system_browser.command_output', command)
    browser = (await SystemBrowserLauncher(Store(tmp_path)).discover())[0]
    assert browser.kind == 'snap'
    assert browser.command == ('/usr/bin/snap', 'run', 'chromium')


def test_profiles_respect_flatpak_and_snap_boundaries_and_block_notifications(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    launcher = SystemBrowserLauncher(Store(tmp_path / 'state'))
    flatpak = SystemBrowser('flatpak:org.chromium.Chromium', 'Chromium', 'flatpak', ('flatpak','run','org.chromium.Chromium'))
    profile = launcher.profile_for(flatpak, 'account1')
    assert profile == tmp_path / 'home/.local/share/ultraroute/ultraprofile'
    launcher.disable_notifications(profile)
    preferences = profile / 'Default/Preferences'
    values = json.loads(preferences.read_text())
    assert values['profile']['default_content_setting_values']['notifications'] == 2
    values['other_setting'] = True
    preferences.write_text(json.dumps(values))
    launcher.disable_notifications(profile)
    assert json.loads(preferences.read_text())['other_setting'] is True
    snap = SystemBrowser('snap:chromium','Chromium','snap',('snap','run','chromium'))
    assert launcher.profile_for(snap,'account2').parts[-5:]==('snap','chromium','common','ultraroute','ultraprofile')


def test_restart_preserves_approval_and_explicit_disconnect_revokes_it(tmp_path):
    store = Store(tmp_path)
    account = store.add_account('deepseek','Persistent')
    browser = SystemBrowser('native:chromium','Chromium','native',('chromium',))
    store.save_browser(account['id'], browser, tmp_path / 'profile')
    store.approve_browser(account['id'])
    restarted = Store(tmp_path)
    assert restarted.account(account['id'])['status'] == 'ready'
    restarted.approve_browser(account['id'],False)
    restarted.status(account['id'],'disconnected')
    assert Store(tmp_path).account(account['id'])['status'] == 'disconnected'


@pytest.mark.asyncio
async def test_closed_tab_recovers_in_same_context_without_panel(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from dataclasses import replace
    from ultraroute.browser import BrowserPool
    from ultraroute.providers import PROVIDERS
    store = Store(tmp_path)
    account = store.add_account('deepseek','Closed tab')
    monkeypatch.setitem(PROVIDERS,'deepseek',replace(PROVIDERS['deepseek'],url='https://fixture.invalid/'))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies([{'name':'session','value':'preserved','url':'https://fixture.invalid/'}])
        await context.route('**/*', lambda r:r.fulfill(status=200,content_type='text/html',body='<textarea></textarea>'))
        page = await context.new_page()
        pool = BrowserPool(store)
        pool.contexts[account['id']] = context
        pool.pages[account['id']] = page
        await page.close()
        recovered = await pool._open(account)
        assert not recovered.is_closed()
        assert recovered.url == 'https://fixture.invalid/'
        assert (await context.cookies())[0]['value'] == 'preserved'
        await browser.close()


@pytest.mark.asyncio
async def test_no_automation_before_login_confirmation(tmp_path, monkeypatch):
    from ultraroute.browser import BrowserPool
    store = Store(tmp_path)
    account = store.add_account('deepseek','Manual login')
    pool = BrowserPool(store)
    async def opened(account, show=False):
        assert show
        return {'port':12345,'browser_name':'Chromium (Flatpak)'}
    monkeypatch.setattr(pool.launcher,'open',opened)
    result = await pool.open_login(account)
    assert result['status'] == 'awaiting_login'
    assert pool.contexts == {} and pool.external_drivers == {}
    assert store.account(account['id'])['status'] == 'awaiting_login'


@pytest.mark.asyncio
async def test_launcher_uses_normal_browser_and_blocks_notifications(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    store = Store(tmp_path / 'store')
    account = store.add_account('deepseek','Normal browser')
    launcher = SystemBrowserLauncher(store)
    browser = SystemBrowser('flatpak:org.chromium.Chromium','Chromium (Flatpak)','flatpak',('flatpak','run','org.chromium.Chromium'))
    async def discover(force=False): return [browser]
    async def endpoint(aid): return 45678
    commands = []
    def spawn(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(launcher,'discover',discover)
    monkeypatch.setattr(launcher,'endpoint',endpoint)
    monkeypatch.setattr(launcher,'available_port',lambda profile:45678)
    monkeypatch.setattr(launcher,'_probe',lambda port:'/devtools/browser/test-owned-browser')
    monkeypatch.setattr('ultraroute.system_browser.subprocess.Popen',spawn)
    result = await launcher.open(account)
    assert result['port'] == 45678
    assert commands[0][:3] == ['flatpak','run','org.chromium.Chromium']
    assert '--disable-notifications' in commands[0]
    assert '--remote-debugging-port=45678' in commands[0]
    assert '--remote-debugging-port=0' not in commands[0]
    assert '--enable-automation' not in commands[0] and '--no-sandbox' not in commands[0]
    assert store.browser_session(account['id'])['approved'] == 0
    await launcher.open(account)
    assert len(commands) == 1  # Reuse a live browser instead of spawning duplicates.


@pytest.mark.asyncio
async def test_reconnect_and_restart_preserve_approved_profile(tmp_path, monkeypatch):
    from dataclasses import replace
    from playwright.async_api import async_playwright
    from ultraroute.browser import BrowserPool
    from ultraroute.providers import PROVIDERS
    store = Store(tmp_path / 'store')
    account = store.add_account('deepseek','Recoverable browser')
    profile = tmp_path / 'browser-profile'
    browser_spec = SystemBrowser('native:test','Chromium test','native',('test-browser',))
    store.save_browser(account['id'],browser_spec,profile)
    store.approve_browser(account['id'])
    monkeypatch.setitem(PROVIDERS,'deepseek',replace(PROVIDERS['deepseek'],url='https://fixture.invalid/'))
    async with async_playwright() as owner:
        owner_contexts = []
        async def relaunch(account):
            context = await owner.chromium.launch_persistent_context(str(profile),headless=True,
                args=['--remote-debugging-address=127.0.0.1','--remote-debugging-port=0'])
            await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body='<textarea></textarea>'))
            await context.pages[0].goto('https://fixture.invalid/')
            owner_contexts.append(context)
            port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
            return {'port':port,'browser_name':'Chromium test'}
        pool = BrowserPool(store)
        monkeypatch.setattr(pool.launcher,'open',relaunch)
        page = await pool._open(account)
        await page.context.add_cookies([{'name':'session','value':'persisted','url':'https://fixture.invalid/','expires':2000000000}])
        await owner_contexts[0].close()
        recovered = await pool._open(account)
        assert not recovered.is_closed()
        assert len(owner_contexts) == 2
        cookies = await recovered.context.cookies()
        assert any(c['name']=='session' and c['value']=='persisted' for c in cookies)
        await pool.shutdown()
        assert not owner_contexts[-1].pages[0].is_closed()
        # A new server process reuses the saved approval and the live browser.
        restarted_store = Store(tmp_path / 'store')
        restarted_pool = BrowserPool(restarted_store)
        assert restarted_store.account(account['id'])['status'] == 'ready'
        recovered_again = await restarted_pool._open(account)
        assert recovered_again.url == 'https://fixture.invalid/'
        await restarted_pool.close(account['id'])
        assert not restarted_store.browser_session(account['id'])['approved']
        with pytest.raises(GatewayError) as error:
            await restarted_pool._open(account)
        assert error.value.code == 'login_required'
        await owner_contexts[-1].close()


def test_primary_profile_shared_across_providers_but_not_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path/'home'))
    store = Store(tmp_path/'store')
    launcher = SystemBrowserLauncher(store)
    browser = SystemBrowser('flatpak:org.chromium.Chromium','Chromium','flatpak',('flatpak','run','org.chromium.Chromium'))
    deepseek = store.add_account('deepseek','DeepSeek')
    primary = launcher.profile_for(browser,deepseek['id'],'deepseek')
    store.save_browser(deepseek['id'],browser,primary)
    qwen = store.add_account('qwen','Qwen')
    assert launcher.profile_for(browser,qwen['id'],'qwen') == primary
    another = store.add_account('deepseek','DeepSeek second account')
    assert launcher.profile_for(browser,another['id'],'deepseek') != primary


@pytest.mark.asyncio
async def test_one_browser_multiple_providers_and_closed_tab_recovery(tmp_path,monkeypatch):
    from dataclasses import replace
    from playwright.async_api import async_playwright
    from ultraroute.browser import BrowserPool
    from ultraroute.providers import PROVIDERS
    store = Store(tmp_path/'store')
    deepseek = store.add_account('deepseek','DeepSeek')
    qwen = store.add_account('qwen','Qwen')
    monkeypatch.setitem(PROVIDERS,'deepseek',replace(PROVIDERS['deepseek'],url='https://deepseek.fixture/'))
    monkeypatch.setitem(PROVIDERS,'qwen',replace(PROVIDERS['qwen'],url='https://qwen.fixture/'))
    async with async_playwright() as owner:
        profile = tmp_path/'browser'
        context = await owner.chromium.launch_persistent_context(str(profile),headless=True,args=['--remote-debugging-port=0'])
        await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body='<textarea></textarea>'))
        first = context.pages[0]
        await first.goto('https://deepseek.fixture/')
        second = await context.new_page()
        await second.goto('https://qwen.fixture/')
        port = int((profile/'DevToolsActivePort').read_text().splitlines()[0])
        pool = BrowserPool(store)
        await pool.attach(deepseek,port)
        await pool.attach(qwen,port)
        assert pool.external_ports[deepseek['id']] == pool.external_ports[qwen['id']]
        await pool.close(deepseek['id'])
        assert not second.is_closed()
        await second.close()
        recovered = await pool._open(qwen)
        assert recovered.url == 'https://qwen.fixture/'
        await pool.shutdown()
        assert not recovered.is_closed()
        await context.close()
