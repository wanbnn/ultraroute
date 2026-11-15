import asyncio
from pathlib import Path

import pytest

from ultraroute import paths
from ultraroute.store import Store
from ultraroute.system_browser import SystemBrowserLauncher


def test_windows_data_stays_outside_tool_environment(tmp_path,monkeypatch):
    monkeypatch.setattr(paths.sys,'platform','win32')
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    assert paths.user_data_root()==tmp_path/'Local/UltraRoute'
    monkeypatch.setenv('ULTRAROUTE_DATA_DIR',str(tmp_path/'override'))
    assert paths.default_data_dir()==tmp_path/'override'


@pytest.mark.asyncio
async def test_windows_chrome_detected_without_path_entry(tmp_path,monkeypatch):
    import ultraroute.system_browser as module
    chrome=tmp_path/'Google/Chrome/Application/chrome.exe'
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    monkeypatch.setattr(module,'windows_browser_paths',lambda:[(chrome,'Google Chrome')])
    monkeypatch.setattr(module.shutil,'which',lambda _:None)
    browser=(await SystemBrowserLauncher(Store(tmp_path/'state')).discover())[0]
    assert browser.command==(str(chrome),)
    assert browser.kind=='native'


@pytest.mark.asyncio
async def test_windows_installs_chrome_with_winget(tmp_path,monkeypatch):
    import ultraroute.system_browser as module
    from ultraroute.system_browser import SystemBrowser
    monkeypatch.setattr(module.sys,'platform','win32')
    monkeypatch.setattr(module.shutil,'which',lambda name:'winget.exe' if name=='winget' else None)
    launcher=SystemBrowserLauncher(Store(tmp_path))
    calls=[]
    installed=False
    async def discover(force=False):
        return [SystemBrowser('chrome','Chrome','native',('chrome.exe',))] if installed else []
    async def command(command,timeout=15):
        nonlocal installed
        calls.append(command)
        installed=True
        return 0,'',''
    monkeypatch.setattr(launcher,'discover',discover)
    monkeypatch.setattr(module,'command_output',command)
    assert (await launcher.ensure_browser()).name=='Chrome'
    assert '--id' in calls[0] and 'Google.Chrome' in calls[0]
    assert '--disable-interactivity' in calls[0]
