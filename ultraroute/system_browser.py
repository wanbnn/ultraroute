"""System Chromium discovery, installation and human-first persistent sessions.

Launching a normal browser does not establish a Playwright/CDP connection.
Only an explicit confirmation grants automation, persisted per dedicated profile.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import GatewayError
from .providers import PROVIDERS
from .paths import user_data_root


@dataclass(frozen=True)
class SystemBrowser:
    id: str
    name: str
    kind: str
    command: tuple[str, ...]

    def public(self):
        return {'id': self.id, 'name': self.name, 'kind': self.kind}


async def command_output(command, timeout=15):
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace')


def windows_browser_paths():
    if sys.platform != 'win32':
        return []
    roots = [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')]
    return [(Path(root) / relative, name) for root in filter(None, roots)
            for relative, name in [('Google/Chrome/Application/chrome.exe', 'Google Chrome'),
                                   ('Chromium/Application/chrome.exe', 'Chromium')]]


def spawn_options():
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200), 'close_fds': True}
    return {'start_new_session': True, 'close_fds': True}


class SystemBrowserLauncher:
    def __init__(self, store):
        self.store = store
        self.cached = []
        self.detected_at = 0
        self.discovery_lock = asyncio.Lock()
        self.install_lock = asyncio.Lock()
        self.launch_locks = {}
        self.open_lock = asyncio.Lock()
        self.processes = {}
        self.install_state = 'idle'

    async def discover(self, force=False):
        async with self.discovery_lock:
            if not force and self.detected_at and time.monotonic() - self.detected_at < 30:
                return self.cached
            found = []
            override = os.getenv('ULTRAROUTE_BROWSER')
            if override and Path(override).is_file() and os.access(override, os.X_OK):
                found.append(SystemBrowser('custom', 'Navegador configurado', 'native', (override,)))
            seen = set()
            for binary, name in [('google-chrome-stable', 'Google Chrome'), ('google-chrome', 'Google Chrome'),
                                  ('chromium', 'Chromium'), ('chromium-browser', 'Chromium')]:
                executable = shutil.which(binary)
                if executable and executable not in seen:
                    kind = 'snap' if '/snap/' in executable else 'native'
                    found.append(SystemBrowser(kind + ':' + binary, name + (' (Snap)' if kind == 'snap' else ''), kind, (executable,)))
                    seen.add(executable)
            for candidate, name in windows_browser_paths():
                executable = str(candidate)
                if candidate.is_file() and executable not in seen:
                    found.append(SystemBrowser('native:' + name.lower().replace(' ', '-'), name, 'native', (executable,)))
                    seen.add(executable)
            flatpak = shutil.which('flatpak')
            if flatpak:
                try:
                    code, listing, _ = await command_output([flatpak, 'list', '--app', '--columns=application'])
                    installed = set(listing.splitlines()) if code == 0 else set()
                    for app_id, name in [('org.chromium.Chromium', 'Chromium (Flatpak)'), ('com.google.Chrome', 'Google Chrome (Flatpak)')]:
                        if app_id in installed:
                            found.append(SystemBrowser('flatpak:' + app_id, name, 'flatpak', (flatpak, 'run', app_id)))
                except (OSError, TimeoutError):
                    pass
            snap = shutil.which('snap')
            if snap and not any(b.kind == 'snap' for b in found):
                try:
                    code, _, _ = await command_output([snap, 'list', 'chromium'], timeout=5)
                    if code == 0:
                        found.append(SystemBrowser('snap:chromium', 'Chromium (Snap)', 'snap', (snap, 'run', 'chromium')))
                except (OSError, TimeoutError):
                    pass
            preference = os.getenv('ULTRAROUTE_SYSTEM_BROWSER')
            if preference:
                found.sort(key=lambda b: b.id != preference)
            self.cached, self.detected_at = found, time.monotonic()
            return found

    async def inventory(self):
        available = await self.discover()
        return {'available': [b.public() for b in available], 'selected': available[0].public() if available else None,
                'install_status': self.install_state,
                'auto_install': 'winget' if sys.platform == 'win32' and shutil.which('winget') else 'flatpak' if shutil.which('flatpak') else 'snap' if shutil.which('snap') else None}

    async def ensure_browser(self):
        available = await self.discover()
        if available:
            return available[0]
        async with self.install_lock:
            available = await self.discover(force=True)
            if available:
                return available[0]
            self.install_state = 'installing'
            try:
                flatpak, snap = shutil.which('flatpak'), shutil.which('snap')
                if sys.platform == 'win32' and shutil.which('winget'):
                    commands = [[shutil.which('winget'), 'install', '--id', 'Google.Chrome', '--exact', '--source', 'winget',
                                 '--accept-package-agreements', '--accept-source-agreements', '--silent', '--disable-interactivity']]
                elif flatpak:
                    commands = [
                        [flatpak, 'remote-add', '--user', '--if-not-exists', 'flathub', 'https://flathub.org/repo/flathub.flatpakrepo'],
                        [flatpak, 'install', '--user', '--noninteractive', '-y', 'flathub', 'org.chromium.Chromium'],
                    ]
                elif snap:
                    commands = [[snap, 'install', 'chromium']]
                else:
                    raise GatewayError('Nenhum Chrome/Chromium encontrado. Instale Chrome ou habilite WinGet (Windows) / Flatpak ou Snap (Linux).', 'browser_install_unavailable', 503)
                for command in commands:
                    code, _, _ = await command_output(command, timeout=900)
                    if code:
                        raise GatewayError('A instalação do Chromium falhou. Confira rede e permissões do Flatpak/Snap.', 'browser_install_failed', 503)
                available = await self.discover(force=True)
                if not available:
                    raise GatewayError('Instalação concluída, mas o Chromium não foi encontrado.', 'browser_install_failed', 503)
                self.install_state = 'installed'
                return available[0]
            except BaseException as exc:
                self.install_state = 'failed'
                if isinstance(exc, (OSError, TimeoutError)):
                    raise GatewayError('Não foi possível concluir a instalação do Chromium. Confira rede e permissões.', 'browser_install_failed', 503) from exc
                raise

    def profile_for(self, browser, aid, provider=None):
        # First account of each provider shares the human-authenticated UltraProfile.
        # A second account of the same provider needs its own cookie jar.
        root = (Path.home() / 'snap/chromium/common/ultraroute' if browser.kind == 'snap'
                else user_data_root())
        primary = root / 'ultraprofile'
        occupied = any(a['id'] != aid and a['provider'] == provider and
                       (self.store.browser_session(a['id']) or {}).get('profile') == str(primary)
                       for a in self.store.accounts()) if provider else False
        profile = root / 'profiles' / aid if occupied else primary
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        return profile

    @staticmethod
    def disable_notifications(profile):
        preferences = Path(profile) / 'Default' / 'Preferences'
        preferences.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            values = json.loads(preferences.read_text()) if preferences.exists() else {}
        except (OSError, ValueError) as exc:
            raise GatewayError('Não foi possível ler as preferências do perfil de navegador.', 'invalid_browser_profile', 503) from exc
        values.setdefault('profile', {}).setdefault('default_content_setting_values', {})['notifications'] = 2
        temporary = preferences.with_suffix('.ultraroute.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(values, file)
        temporary.replace(preferences)

    @staticmethod
    def _probe(port):
        if not 1024 <= port <= 65535:
            return None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=.6) as response:
                info = json.loads(response.read(65536))
            path = urlparse(info.get('webSocketDebuggerUrl', '')).path
            return path if path.startswith('/devtools/browser/') else None
        except (OSError, ValueError):
            return None

    @classmethod
    def _endpoint(cls, profile):
        """Match the saved browser UUID so a reused port cannot attach another browser."""
        try:
            metadata = Path(profile) / '.ultraroute-cdp.json'
            if metadata.exists():
                saved = json.loads(metadata.read_text())
                port, expected = int(saved['port']), saved['browser_path']
            else:
                # Compatibility with profiles launched by the early port=0 implementation.
                lines = (Path(profile) / 'DevToolsActivePort').read_text().splitlines()
                port, expected = int(lines[0]), lines[1]
            return port if cls._probe(port) == expected else None
        except (OSError, ValueError, IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def save_endpoint(profile, port, browser_path):
        path = Path(profile) / '.ultraroute-cdp.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump({'port': port, 'browser_path': browser_path}, file)

    @staticmethod
    def available_port(profile):
        preferred = 9222 if Path(profile).name == 'ultraprofile' else 9223
        try:
            preferred = int(json.loads((Path(profile) / '.ultraroute-cdp.json').read_text())['port'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        ports = [9222] if Path(profile).name == 'ultraprofile' else [preferred, *range(9223, 9323)]
        for port in ports:
            if not 1024 <= port <= 65535:
                continue
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                    return port
                except OSError:
                    if Path(profile).name == 'ultraprofile':
                        raise GatewayError('A porta 9222 do UltraProfile já está em uso por outro processo. Feche esse processo e tente abrir o navegador novamente.', 'browser_port_in_use', 409)
                    continue
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            # Pass the actual chosen port to Chromium, never the value zero.
            return probe.getsockname()[1]

    async def endpoint(self, aid):
        session = self.store.browser_session(aid)
        if not session:
            return None
        return await asyncio.to_thread(self._endpoint, session['profile'])

    async def open(self, account, show=False):
        aid = account['id']
        async with self.open_lock:
            return await self._open_serial(account, show)

    async def _open_serial(self, account, show=False):
        aid = account['id']
        async with self.launch_locks.setdefault(aid, asyncio.Lock()):
            session = self.store.browser_session(aid)
            if session:
                port = await self.endpoint(aid)
                if port:
                    if show:
                        # Chromium forwards this URL to the existing dedicated profile.
                        # This uses normal browser startup, not a CDP connection.
                        command = [*json.loads(session['command']), '--user-data-dir=' + session['profile'],
                                   PROVIDERS[account['provider']].url]
                        with open(os.devnull, 'w') as output:
                            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                             **spawn_options())
                    return {'port': port, 'browser_name': session['browser_name'], 'reused': True}
            available = await self.discover()
            browser = next((b for b in available if session and b.id == session['browser_id']), None)
            browser = browser or await self.ensure_browser()
            if session and browser.id != session['browser_id']:
                # Profile locations are package-specific; do not silently lose cookies.
                raise GatewayError('O navegador deste perfil foi removido. Reinstale-o para recuperar a sessão.', 'browser_missing', 503)
            profile = Path(session['profile']) if session else self.profile_for(browser, aid, account['provider'])
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            existing_port = await asyncio.to_thread(self._endpoint, profile)
            if existing_port:
                self.store.save_browser(aid, browser, profile)
                if show:
                    command = [*browser.command, '--user-data-dir=' + str(profile), PROVIDERS[account['provider']].url]
                    with open(os.devnull, 'w') as output:
                        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                         **spawn_options())
                return {'port': existing_port, 'browser_name': browser.name, 'reused': True}
            self.disable_notifications(profile)
            self.store.save_browser(aid, browser, profile)
            (profile / 'DevToolsActivePort').unlink(missing_ok=True)
            port = self.available_port(profile)
            command = [*browser.command, '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                       '--user-data-dir=' + str(profile), '--disable-notifications', '--no-first-run', '--no-default-browser-check',
                       PROVIDERS[account['provider']].url]
            log_path = profile / 'ultraroute-launch.log'
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'w') as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               **spawn_options())
            except OSError as exc:
                raise GatewayError('Não foi possível iniciar o navegador instalado.', 'browser_unavailable', 503) from exc
            self.processes[aid] = process
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                browser_path = await asyncio.to_thread(self._probe, port)
                if browser_path:
                    self.save_endpoint(profile, port, browser_path)
                    return {'port': port, 'browser_name': browser.name, 'reused': False}
                if process.poll() is not None:
                    raise GatewayError('O navegador encerrou durante a abertura. Confira o ambiente gráfico e o log local do perfil.', 'browser_unavailable', 503)
                await asyncio.sleep(.25)
            raise GatewayError('O navegador não disponibilizou a sessão local a tempo. Confira a janela e tente novamente.', 'browser_start_timeout', 503)

    async def remove(self, aid):
        session = self.store.browser_session(aid)
        if not session:
            return
        if any(a['id'] != aid and (self.store.browser_session(a['id']) or {}).get('profile') == session['profile']
               for a in self.store.accounts()):
            # Other providers still own sessions in the shared UltraProfile.
            return
        process = self.processes.get(aid)
        if (process and process.poll() is None) or await self.endpoint(aid):
            raise GatewayError('Feche a janela desta conta antes de excluir o perfil.', 'browser_still_open', 409)
        shutil.rmtree(session['profile'], ignore_errors=True)
