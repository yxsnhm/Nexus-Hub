from bridge_base import BaseBridge

class GeminiBridge(BaseBridge):
    def __init__(self):
        super().__init__(
            api_key_env="GEMINI_API_KEY",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model="gemini-2.0-flash",
            name="Gemini"
        )