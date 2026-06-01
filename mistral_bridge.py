from bridge_base import BaseBridge

class MistralBridge(BaseBridge):
    def __init__(self):
        super().__init__(
            api_key_env="MISTRAL_API_KEY",
            base_url="https://api.mistral.ai/v1/chat/completions",
            model="mistral-medium",
            name="Mistral"
        )