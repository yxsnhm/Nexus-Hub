from bridge_base import BaseBridge

class QwenBridge(BaseBridge):
    def __init__(self):
        super().__init__(
            api_key_env="QWEN_API_KEY",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            model="qwen-plus",
            name="千问"
        )