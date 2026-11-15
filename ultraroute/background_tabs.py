"""Create Chromium request tabs without activating its window.

BrowserContext.new_page() opens an active target. On GNOME, that focus request
can trigger the '<application> is ready' desktop attention notification.
"""
import asyncio
import time

from .errors import GatewayError


async def new_background_page(context):
    browser = context.browser
    if browser is None:
        raise GatewayError('Navegador não disponível para abrir uma aba em segundo plano.', 'browser_unavailable', 503)
    session = await browser.new_browser_cdp_session()
    target_id = None
    success = False
    try:
        params = {'url': 'about:blank', 'background': True, 'focus': False, 'newWindow': False}
        anchor = next((page for page in context.pages if not page.is_closed()), None)
        if anchor:
            probe = await context.new_cdp_session(anchor)
            try:
                info = (await probe.send('Target.getTargetInfo'))['targetInfo']
                if info.get('browserContextId'):
                    params['browserContextId'] = info['browserContextId']
            finally:
                await probe.detach()
        else:
            # A managed normal browser has one default context. Also support the
            # sole incognito context used by local regression tests.
            contexts = (await session.send('Target.getBrowserContexts'))['browserContextIds']
            if contexts:
                if len(contexts) != 1 or len(browser.contexts) != 1:
                    raise GatewayError('Não foi possível identificar o contexto do navegador.', 'browser_context_unavailable', 503)
                params['browserContextId'] = contexts[0]
        target_id = (await session.send('Target.createTarget', params))['targetId']
        checked = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Match the target ID rather than the next 'page' event: independent
            # providers may create background tabs concurrently in UltraProfile.
            for page in context.pages:
                if page in checked or page.is_closed():
                    continue
                checked.add(page)
                probe = await context.new_cdp_session(page)
                try:
                    info = (await probe.send('Target.getTargetInfo'))['targetInfo']
                    if info['targetId'] == target_id:
                        success = True
                        return page
                finally:
                    await probe.detach()
            await asyncio.sleep(.025)
        raise GatewayError('Tempo limite abrindo a aba em segundo plano.', 'browser_tab_timeout', 503)
    finally:
        if target_id and not success:
            try:
                await session.send('Target.closeTarget', {'targetId': target_id})
            except Exception:
                pass
        await session.detach()
