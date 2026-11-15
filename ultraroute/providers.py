from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    url: str
    color: str
    composer: tuple[str, ...]
    assistant: tuple[str, ...]
    stop: tuple[str, ...]
    send: tuple[str, ...]
    model: str = 'web'

    @property
    def model_id(self):
        return f'{self.id}/{self.model}'


# DOM adapters: authenticated layouts require verification against the user's account.
# No private HTTP endpoints or authentication tokens are assumed.
PROVIDERS = {
    p.id: p for p in [
        Provider("chatgpt", "ChatGPT", "https://chatgpt.com/", "#70bda2",
                 ('#prompt-textarea', 'textarea[placeholder*="Message"]'),
                 ('[data-message-author-role="assistant"]',),
                 ('button[data-testid="stop-button"]', 'button[aria-label="Stop generating"]'),
                 ('button[data-testid="send-button"]', 'button[aria-label="Send prompt"]')),
        Provider("qwen", "Qwen", "https://chat.qwen.ai/", "#a99bff",
                 ('textarea#chat-input', 'textarea[placeholder]', '[contenteditable="true"][role="textbox"]'),
                 ('.chat-assistant .markdown', '[data-role="assistant"]', '.response-message .markdown'),
                 ('button[aria-label="Stop"]', '[data-testid="stop-button"]'),
                 ('button[aria-label="Send"]', 'button[type="submit"]')),
        Provider("kimi", "Kimi", "https://www.kimi.com/en", "#91b8ff",
                 ('.chat-input-editor[contenteditable="true"]', '[contenteditable="true"][data-lexical-editor="true"]', 'textarea'),
                 ('.segment-assistant .markdown', '.segment-assistant .markdown-container', '[data-role="assistant"]'),
                 ('button[aria-label="Stop"]', '.stop-button'),
                 ('.send-button', 'button[aria-label="Send"]')),
        Provider("glm", "GLM", "https://z.ai/chat", "#eac878",
                 ('#chat-input', 'textarea', '[contenteditable="true"][role="textbox"]'),
                 ('.chat-assistant .markdown', '[data-message-author-role="assistant"]', '.message-assistant .markdown'),
                 ('button[aria-label="Stop"]', 'button[title="Stop generating"]'),
                 ('button[type="submit"]', 'button[aria-label="Send message"]')),
        Provider("deepseek", "DeepSeek", "https://chat.deepseek.com/", "#7f9eff",
                 ('#chat-input', 'textarea[placeholder]', 'textarea'),
                 ('.ds-markdown', '[data-role="assistant"]'),
                 ('button[aria-label="Stop"]', '[data-testid="stop-button"]'),
                 ('button[aria-label="Send"]', '[role="button"][aria-label="Send"]')),
        Provider("aistudio", "Google AI Studio",
                 "https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen", "#8ab4f8",
                 ('textarea[aria-label="Enter a prompt"]', 'ms-autosize-textarea textarea', 'textarea[aria-label*="prompt" i]',
                  'textarea[placeholder*="Start typing" i]', 'textarea[placeholder*="Type something" i]'),
                 ('ms-chat-turn .model-prompt-container ms-text-chunk ms-cmark-node.cmark-node:not(ms-thought-chunk *)',),
                 ('button[aria-label*="Stop" i]', 'button[mattooltip*="Stop" i]'),
                 ('button[aria-label="Run"]', 'button[aria-label="Run prompt"]',
                  'button.run-button', 'ms-run-button button'),
                 model='gemini-4-hydrogen'),
    ]
}
