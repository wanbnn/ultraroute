"""Read AI Studio answer chunks, excluding thought panels and interface chrome.

Observed on authenticated AI Studio: model-prompt-container also holds the model
header, clock, loading indicator and ms-thought-chunk. None is answer content.
Nested ms-cmark-node elements are markdown leaves, not separate messages.
"""

SNAPSHOT = r'''(baseline) => {
    const containers = [...document.querySelectorAll('ms-chat-turn .model-prompt-container')].slice(baseline);
    const responses = [];
    for (const container of containers) {
        const roots = [...container.querySelectorAll('ms-text-chunk ms-cmark-node')].filter(node => {
            if (node.closest('ms-thought-chunk, .thought-panel, [data-is-thought="true"]')) return false;
            // Keep each complete rendered markdown root once, never its nested leaves.
            return !node.parentElement.closest('ms-cmark-node');
        });
        const text = roots.map(root => {
            const clone = root.cloneNode(true);
            clone.querySelectorAll('button, ms-code-block-toolbar, .code-block-actions, .copy-button, [aria-hidden="true"]')
                .forEach(node => node.remove());
            // textContent loses paragraph boundaries. A detached innerText also loses
            // layout, so insert separators structurally before extracting plain text.
            clone.querySelectorAll('p, pre, blockquote, ul, ol, h1, h2, h3, h4, h5, h6')
                .forEach(node => node.append(document.createTextNode('\n\n')));
            clone.querySelectorAll('br').forEach(node => node.replaceWith(document.createTextNode('\n')));
            clone.querySelectorAll('li').forEach(node => node.append(document.createTextNode('\n')));
            return (clone.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
        }).filter(Boolean).join('\n\n');
        if (text) responses.push(text);
    }
    return responses.join('\n\n');
}'''


async def baseline(page):
    return await page.locator('ms-chat-turn .model-prompt-container').count()


async def answer_text(page, previous_containers):
    return await page.evaluate(SNAPSHOT, previous_containers)


async def error_text(page, previous_containers):
    return await page.evaluate(r'''(baseline) => {
        const containers = [...document.querySelectorAll('ms-chat-turn .model-prompt-container')].slice(baseline);
        for (const container of containers) {
            const error = container.querySelector('.model-error');
            if (error && error.getClientRects().length) {
                return (error.querySelector('span')?.textContent || error.textContent || '').trim().slice(0, 1000);
            }
        }
        return '';
    }''', previous_containers)
