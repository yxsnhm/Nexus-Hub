from bridge_base import BaseBridge

class LlamaBridge(BaseBridge):
    def __init__(self):
        super().__init__(
            api_key_env="LLAMA_API_KEY",
            base_url="https://api.llama-api.com/chat/completions",
            model="llama3-70b",
            name="Llama"
        )