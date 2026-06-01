from bridge_base import BaseBridge
import requests

class ClaudeBridge(BaseBridge):
    def __init__(self):
        super().__init__(
            api_key_env="CLAUDE_API_KEY",
            base_url="https://api.anthropic.com/v1/messages",
            model="claude-3-haiku-20240307",
            name="Claude"
        )

    def _call(self, messages, temperature=0.7, max_tokens=1000):
        if not self.api_key:
            print(f"⚠️ {self.name} API key 未设置")
            return None
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        system = None
        claude_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                claude_msgs.append({"role": m["role"], "content": m["content"]})
        data = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "messages": claude_msgs}
        if system:
            data["system"] = system
        try:
            resp = requests.post(self.base_url, headers=headers, json=data, timeout=120)
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"].strip()
                return {"choices": [{"message": {"content": text}}]}
            else:
                print(f"Claude错误: {resp.status_code} {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"Claude调用失败: {e}")
            return None