import asyncio
import shutil
import time
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from .providers import PROVIDERS
from . import aistudio
from .background_tabs import new_background_page
from .errors import GatewayError
from .system_browser import SystemBrowserLauncher


class BrowserPool:
    def __init__(self, store, timeout=180):
        self.store = store
        self.timeout = timeout
        self.contexts = {}
        self.pages = {}
        self.locks = {}
        self.playwright = None
        self.start_lock = asyncio.Lock()
        self.external_drivers = {}
        self.external_ports = {}
        self.launcher = SystemBrowserLauncher(store)
        self.recovery_lock = asyncio.Lock()
        self.attachment_lock = asyncio.Lock()

    def is_connected(self, aid):
        context = self.contexts.get(aid)
        return bool(context and (context.browser is None or context.browser.is_connected()))

    def lock(self, aid):
        return self.locks.setdefault(aid, asyncio.Lock())

    async def start(self):
        async with self.start_lock:
            if not self.playwright:
                self.playwright = await async_playwright().start()

    async def _open(self, account):
        """Recover tabs and previously approved managed browsers without relogin."""
        aid = account['id']
        context = self.contexts.get(aid)
        if context:
            page = self.pages.get(aid)
            connected = context.browser is None or context.browser.is_connected()
            if connected and page and not page.is_closed():
                return page
            try:
                # A tab closing does not invalidate the browser's authenticated context.
                page = await new_background_page(context)
                await page.goto(PROVIDERS[account['provider']].url, wait_until='domcontentloaded', timeout=60000)
                self.pages[aid] = page
                return page
            except Exception:
                await self._close(aid)
        session = self.store.browser_session(aid)
        if not session or not session['approved']:
            raise GatewayError('Abra o login no painel e confirme sua conta.', 'login_required', 503)
        # One recovery at a time also prevents two accounts claiming a CDP port.
        async with self.recovery_lock:
            result = await self.launcher.open(account)
            await self._attach(account, result['port'], open_provider=True)
            return self.pages[aid]

    async def open_login(self, account):
        aid = account['id']
        if self.lock(aid).locked():
            raise GatewayError('Conta ocupada.', 'account_busy', 409)
        async with self.lock(aid):
            # Human-first: no Playwright connection exists during this login stage.
            await self._close(aid)
            self.store.approve_browser(aid, False)
            result = await self.launcher.open(account, show=True)
            self.store.approve_browser(aid, False)
            self.store.status(aid, 'awaiting_login')
        return {'status': 'awaiting_login', 'browser': result['browser_name'],
                'message': result['browser_name'] + ' aberto. Faça login e clique em “Já entrei · conectar”.'}

    async def attach(self, account, port):
        aid = account['id']
        if self.lock(aid).locked():
            raise GatewayError('Conta ocupada.', 'account_busy', 409)
        async with self.lock(aid):
            if aid in self.contexts:
                raise GatewayError('Desconecte a sessão atual antes de conectar outro navegador.', 'already_connected', 409)
            await self._attach(account, port)
            self.store.status(aid, 'awaiting_login')
        return {'status': 'awaiting_login', 'message': 'Navegador conectado. Confirme o login para verificar o campo de mensagem.'}

    async def _attach(self, account, port, open_provider=False):
        async with self.attachment_lock:
            await self._attach_unlocked(account, port, open_provider)

    async def _attach_unlocked(self, account, port, open_provider=False):
        aid = account['id']
        if any(p == port and other != aid and
               (self.store.account(other) or {}).get('provider') == account['provider']
               for other, p in self.external_ports.items()):
            raise GatewayError('Esse navegador já está conectado a outra conta deste provedor. Use outro perfil para duas contas do mesmo site.', 'browser_in_use', 409)
        driver = await async_playwright().start()
        try:
            browser = await driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=15000)
            if not browser.contexts:
                raise GatewayError('Navegador sem contexto padrão.', 'browser_unavailable', 409)
            context = browser.contexts[0]
            from urllib.parse import urlparse
            host = urlparse(PROVIDERS[account['provider']].url).hostname
            page = next((p for p in context.pages if urlparse(p.url).hostname == host), None)
            if not page and open_provider:
                page = await new_background_page(context)
                await page.goto(PROVIDERS[account['provider']].url, wait_until='domcontentloaded', timeout=60000)
            if not page:
                raise GatewayError('Abra o site do provedor e faça login nesse navegador antes de conectar.', 'provider_tab_missing', 409)
            self.external_drivers[aid] = driver
            self.external_ports[aid] = port
            self.contexts[aid] = context
            self.pages[aid] = page
        except Exception as exc:
            await driver.stop()
            if isinstance(exc, GatewayError):
                raise
            raise GatewayError('Não foi possível conectar ao navegador local. Confira se ele ainda está aberto.', 'cdp_unavailable', 503) from exc

    async def first_visible(self, page, selectors):
        for selector in selectors:
            locator = page.locator(selector)
            for i in range(await locator.count()):
                item = locator.nth(i)
                if await item.is_visible():
                    return item
        return None

    async def confirm(self, account):
        aid = account['id']
        if self.lock(aid).locked():
            raise GatewayError('Conta ocupada.', 'account_busy', 409)
        async with self.lock(aid):
            page = self.pages.get(aid)
            if not page or page.is_closed():
                session = self.store.browser_session(aid)
                port = await self.launcher.endpoint(aid) if session else None
                if port:
                    await self._close(aid)
                    await self._attach(account, port, open_provider=True)
                    page = self.pages[aid]
                else:
                    raise GatewayError('Abra o navegador e faça login primeiro.', 'login_required', 409)
            composer = None
            for _ in range(20):
                composer = await self.first_visible(page, PROVIDERS[account['provider']].composer)
                if composer:
                    break
                await asyncio.sleep(.25)
            if not composer:
                raise GatewayError('Campo de mensagem não encontrado. Conclua login/captcha ou ajuste o adaptador.', 'composer_not_found', 409)
            managed_port = await self.launcher.endpoint(aid)
            if managed_port and managed_port == self.external_ports.get(aid):
                self.store.approve_browser(aid, True)
            self.store.status(aid, 'ready')
        return {'status': 'ready', 'verification': 'user_confirmed', 'message': 'Sessão conectada. Você já pode testar uma mensagem no Playground.'}

    async def _close(self, aid):
        context = self.contexts.pop(aid, None)
        self.pages.pop(aid, None)
        driver = self.external_drivers.pop(aid, None)
        self.external_ports.pop(aid, None)
        if driver:
            # End our CDP connection without closing the owner's browser/tabs.
            await driver.stop()
            return
        if context:
            try:
                await context.close()
            except Exception:
                pass

    async def close(self, aid):
        if self.lock(aid).locked():
            raise GatewayError('Aguarde a requisição em andamento.', 'account_busy', 409)
        async with self.lock(aid):
            await self._close(aid)
            self.store.approve_browser(aid, False)
            self.store.status(aid, 'disconnected')

    async def remove(self, aid):
        if self.lock(aid).locked():
            raise GatewayError('Aguarde a requisição em andamento.', 'account_busy', 409)
        async with self.lock(aid):
            await self.launcher.remove(aid)
            await self._close(aid)
            shutil.rmtree(self.store.root / 'profiles' / aid, ignore_errors=True)
            self.store.remove(aid)

    async def shutdown(self):
        for aid in list(self.contexts):
            await self._close(aid)
        if self.playwright:
            await self.playwright.stop()

    @asynccontextmanager
    async def reserve(self, provider):
        candidates = [a for a in self.store.accounts() if a['provider'] == provider and a['status'] == 'ready']
        if not candidates:
            raise GatewayError('Nenhuma conta pronta para este provedor. Abra o painel e conecte uma conta.', 'login_required', 503)
        account = next((a for a in candidates if not self.lock(a['id']).locked()), None)
        if not account:
            raise GatewayError('Todas as contas deste provedor estão ocupadas. Tente novamente.', 'account_busy', 429)
        lock = self.lock(account['id'])
        await lock.acquire()
        try:
            yield account
        finally:
            lock.release()

    async def generate(self, account, prompt):
        aid, provider = account['id'], PROVIDERS[account['provider']]
        started = time.monotonic()
        status = 'error'
        page = None
        try:
            await self._open(account)
            # New tab isolates each API conversation, sharing only this account's cookies.
            page = await new_background_page(self.contexts[aid])
            await page.goto(provider.url, wait_until='domcontentloaded', timeout=60000)
            deadline = time.monotonic() + 25
            composer = None
            while time.monotonic() < deadline:
                composer = await self.first_visible(page, provider.composer)
                if composer:
                    break
                await asyncio.sleep(.5)
            if not composer:
                self.store.approve_browser(aid, False)
                self.store.status(aid, 'awaiting_login', 'Sessão expirada ou interface alterada.')
                raise GatewayError('Campo de mensagem indisponível. Confira login e adaptador no painel.', 'login_required', 503)
            baseline = {s: await page.locator(s).count() for s in provider.assistant}
            studio_baseline = await aistudio.baseline(page) if provider.id == 'aistudio' else None
            await composer.fill(prompt)
            sender = await self.first_visible(page, provider.send)
            if sender and await sender.is_enabled():
                await sender.click()
            else:
                await composer.press('Enter')
            deadline = time.monotonic() + self.timeout
            emitted, last, changed = '', '', time.monotonic()
            seen_stop = False
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise GatewayError('Janela de geração fechada.', 'browser_closed', 503)
                text = ''
                if provider.id == 'aistudio':
                    upstream_error = await aistudio.error_text(page, studio_baseline)
                    if upstream_error:
                        denied = any(word in upstream_error.lower() for word in ('permission', 'permissão', 'access denied'))
                        raise GatewayError('Google AI Studio: ' + upstream_error,
                                           'provider_permission_denied' if denied else 'provider_error',
                                           403 if denied else 502)
                    text = await aistudio.answer_text(page, studio_baseline)
                else:
                    for selector in provider.assistant:
                        messages = page.locator(selector)
                        if await messages.count() > baseline[selector]:
                            text = await messages.last.inner_text()
                            break
                stopping = await self.first_visible(page, provider.stop)
                if stopping:
                    seen_stop = True
                if text != last:
                    last, changed = text, time.monotonic()
                # Forward DOM deltas immediately; never buffer the full answer.
                # Reject rewrites of already transmitted text.
                stable = time.monotonic() - changed
                if text and text != emitted:
                    if not text.startswith(emitted):
                        raise GatewayError('O site reescreveu conteúdo já transmitido. Confira o adaptador.', 'non_monotonic_response')
                    yield text[len(emitted):]
                    emitted = text
                if text and not stopping and stable >= (2 if seen_stop else 5):
                    if text != emitted:
                        if not text.startswith(emitted):
                            raise GatewayError('Resposta instável do provedor.', 'non_monotonic_response')
                        yield text[len(emitted):]
                    status = 'ok'
                    return
                await asyncio.sleep(.25)
            raise GatewayError('Tempo limite aguardando resposta. Confira a janela, limites da conta e seletores do adaptador.', 'provider_timeout', 504)
        except GatewayError:
            raise
        except (asyncio.CancelledError, GeneratorExit):
            status = 'cancelled'
            raise
        except Exception as exc:
            raise GatewayError('Falha no navegador do provedor. Confira a sessão no painel.', 'browser_error', 502) from exc
        finally:
            if page and not page.is_closed():
                await page.close()
            self.store.record(account['provider'], aid, status, time.monotonic() - started)
