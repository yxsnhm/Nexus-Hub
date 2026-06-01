import os, json, requests

class CozeBridge:
    def __init__(self):
        self.api_key = os.getenv("COZE_API_KEY") or "sat_TlFY8iydjiYwlkBUVUSQZ8Te50lu50uh38VidzepRWME9G9TJDjLUTG1OzCa4uyQ"
        self.bot_id = os.getenv("COZE_BOT_ID") or "7642973834605609001"
        self.name = "Coze"

    def _call(self, prompt):
        url = "https://api.coze.cn/v3/chat"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data = {"bot_id": self.bot_id, "user_id": "nexus", "stream": True, "auto_save_history": False,
                "additional_messages": [{"role": "user", "content": prompt, "content_type": "text"}]}
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=180)
            result = []
            for line in resp.iter_lines():
                if line:
                    txt = line.decode("utf-8", errors="ignore").strip()
                    if txt.startswith("data:"):
                        try:
                            msg = json.loads(txt[5:])
                            if msg.get("type") == "answer":
                                content = msg.get("content") or msg.get("reasoning_content", "")
                                if content: result.append(content)
                        except: pass
            return "".join(result)
        except Exception as e:
            print(f"Coze failed: {e}")
        return None

    def generate_code(self, prompt):
        return self._call(f"Write Python code only for: {prompt}")
