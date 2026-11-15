import asyncio
import time
from dataclasses import replace

import pytest
from playwright.async_api import async_playwright

from ultraroute.browser import BrowserPool
from ultraroute.providers import PROVIDERS
from ultraroute.store import Store


@pytest.mark.asyncio
async def test_browser_dom_deltas_before_generation_finished(tmp_path, monkeypatch):
    # Local deterministic website; validates actual Playwright -> DOM -> generator.
    html = '''<!doctype html><meta charset="utf-8"><textarea id="composer"></textarea><button id="send">Send</button>
    <script>document.querySelector('#send').onclick=()=>{
        const stop=document.createElement('button');stop.id='stop';document.body.append(stop);
        const reply=document.createElement('div');reply.className='assistant';document.body.append(reply);
        reply.textContent='first';
        setTimeout(()=>reply.textContent+=' second',700);
        setTimeout(()=>{reply.textContent+=' third';stop.remove();window.finished=true},1400);
    };</script>'''
    store = Store(tmp_path)
    account = store.add_account('deepseek', 'Local DOM test')
    provider = replace(PROVIDERS['deepseek'], url='https://fixture.invalid/',
                       composer=('#composer',), assistant=('.assistant',), stop=('#stop',), send=('#send',))
    monkeypatch.setitem(PROVIDERS, 'deepseek', provider)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
        login_page = await context.new_page()
        pool = BrowserPool(store, timeout=10)
        pool.contexts[account['id']] = context
        pool.pages[account['id']] = login_page

        async def already_open(account):
            return login_page
        monkeypatch.setattr(pool, '_open', already_open)
        chunks = []
        stream = pool.generate(account, 'Hello')
        first = await anext(stream)
        chunks.append(first)
        assert first == 'first'
        generation_page = next(p for p in context.pages if p is not login_page)
        assert await generation_page.evaluate('Boolean(window.finished)') is False
        async for chunk in stream:
            chunks.append(chunk)
        assert ''.join(chunks) == 'first second third'
        assert len(chunks) >= 2
        assert generation_page.is_closed()
        assert store.stats()['successful'] == 1
        await browser.close()


@pytest.mark.asyncio
async def test_external_cdp_disconnect_preserves_owners_browser(tmp_path, monkeypatch):
    store = Store(tmp_path / 'store')
    account = store.add_account('deepseek', 'External')
    monkeypatch.setitem(PROVIDERS, 'deepseek', replace(PROVIDERS['deepseek'], url='https://fixture.invalid/'))
    async with async_playwright() as owner:
        profile = tmp_path / 'owner-browser'
        context = await owner.chromium.launch_persistent_context(str(profile), headless=True,
            args=['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0'])
        await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<textarea></textarea>'))
        page = context.pages[0]
        await page.goto('https://fixture.invalid/')
        port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
        pool = BrowserPool(store)
        result = await pool.attach(account, port)
        assert result['status'] == 'awaiting_login'
        second = store.add_account('deepseek', 'Other')
        from ultraroute.browser import GatewayError
        with pytest.raises(GatewayError) as error:
            await pool.attach(second, port)
        assert error.value.code == 'browser_in_use'
        await pool.close(account['id'])
        assert not page.is_closed()
        assert await page.evaluate('1 + 1') == 2
        await context.close()


@pytest.mark.asyncio
async def test_aistudio_stream_excludes_timer_thoughts_and_nested_markdown(tmp_path, monkeypatch):
    html = '''<!doctype html><meta charset="utf-8"><textarea id="composer"></textarea><button id="send">Run</button>
    <script>document.querySelector('#send').onclick=()=>{
      const stop=document.createElement('button');stop.id='stop';document.body.append(stop);
      const turn=document.createElement('ms-chat-turn');
      turn.innerHTML=`<div class="model-prompt-container"><div class="header">Model <span id="timer">00:20</span></div>
        <ms-chat-loading-indicator>Pensando</ms-chat-loading-indicator>
        <ms-thought-chunk><ms-text-chunk><ms-cmark-node class="cmark-node">Do not transmit this panel</ms-cmark-node></ms-text-chunk></ms-thought-chunk>
        <ms-prompt-chunk id="answer"></ms-prompt-chunk></div>`;
      document.body.append(turn);
      let tick=20;const timer=setInterval(()=>document.querySelector('#timer').textContent='00:'+ ++tick,100);
      setTimeout(()=>document.querySelector('#answer').innerHTML=`<ms-text-chunk><div><ms-cmark-node class="cmark-node"><p><ms-cmark-node>Resposta inicial.</ms-cmark-node></p></ms-cmark-node></div></ms-text-chunk>`,700);
      setTimeout(()=>document.querySelector('#answer ms-cmark-node.cmark-node').innerHTML=`<p><ms-cmark-node>Resposta inicial.</ms-cmark-node></p><p><ms-cmark-node>Segundo parágrafo.</ms-cmark-node></p>`,1500);
      setTimeout(()=>{clearInterval(timer);stop.remove();document.querySelector('ms-chat-loading-indicator').remove();window.finished=true},2100);
    };</script>'''
    store=Store(tmp_path)
    account=store.add_account('aistudio','Studio')
    monkeypatch.setitem(PROVIDERS,'aistudio',replace(PROVIDERS['aistudio'],url='https://studio.fixture/',
        composer=('#composer',),send=('#send',),stop=('#stop',)))
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        context=await browser.new_context()
        await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html))
        anchor=await context.new_page()
        await anchor.goto('https://studio.fixture/')
        pool=BrowserPool(store,timeout=10)
        pool.contexts[account['id']]=context
        pool.pages[account['id']]=anchor
        stream=pool.generate(account,'Hello')
        chunks=[await anext(stream)]
        assert chunks[0]=='Resposta inicial.'
        page=next(p for p in context.pages if p is not anchor)
        assert not await page.evaluate('Boolean(window.finished)')
        async for chunk in stream:chunks.append(chunk)
        assert ''.join(chunks)=='Resposta inicial.\n\nSegundo parágrafo.'
        assert len(chunks)>=2
        assert store.stats()['successful']==1
        await browser.close()


@pytest.mark.asyncio
async def test_aistudio_permission_error_is_identified_without_content(tmp_path):
    from ultraroute import aistudio
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        page=await browser.new_page()
        await page.set_content('''<ms-chat-turn><div class="model-prompt-container">
          <div class="model-error"><span>Permissão negada. Verifique as permissões da sua conta e as configurações do projeto.</span></div>
        </div></ms-chat-turn>''')
        assert (await aistudio.error_text(page,0)).startswith('Permissão negada.')
        assert await aistudio.answer_text(page,0)==''
        assert await aistudio.error_text(page,1)==''
        await browser.close()


@pytest.mark.asyncio
async def test_request_tab_requests_background_without_window_activation(monkeypatch):
    from ultraroute.background_tabs import new_background_page
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        context=await browser.new_context()
        anchor=await context.new_page()
        await anchor.goto('data:text/html,<title>Anchor</title>')
        await anchor.bring_to_front()
        commands=[]
        original=browser.new_browser_cdp_session
        async def recorded_session():
            session=await original()
            send=session.send
            async def record(method,params=None):
                commands.append((method,params))
                return await send(method,params)
            monkeypatch.setattr(session,'send',record)
            return session
        monkeypatch.setattr(browser,'new_browser_cdp_session',recorded_session)
        page=await new_background_page(context)
        await page.goto('data:text/html,<input><button onclick="document.body.dataset.sent=1">Send</button>')
        await page.locator('input').fill('Test')
        await page.locator('button').click()
        assert await page.evaluate('document.body.dataset.sent')=='1'
        params=next(params for method,params in commands if method=='Target.createTarget')
        assert params['background'] is True and params['focus'] is False
        assert params['newWindow'] is False
        assert not any(method in ('Target.activateTarget','Page.bringToFront') for method,_ in commands)
        await page.close()
        await browser.close()
