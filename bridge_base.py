import os, requests 
from dotenv import load_dotenv 
load_dotenv() 
 
class BaseBridge: 
    def __init__(self, api_key_env, base_url, model, name): 
        self.api_key = os.getenv(api_key_env) 
        self.base_url = base_url 
        self.model = model 
        self.name = name 
 
    def _call(self, messages, temperature=0.7, max_tokens=1000): 
        if not self.api_key: 
            print(f"{self.name} API key not set") 
            return None 
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"} 
        data = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens} 
        try: 
            resp = requests.post(self.base_url, json=data, headers=headers, timeout=60) 
            if resp.status_code == 200: 
                return resp.json() 
            else: 
                print(f"{self.name} API error: {resp.status_code}") 
                return None 
        except Exception as e: 
            print(f"{self.name} request failed: {e}") 
            return None 
