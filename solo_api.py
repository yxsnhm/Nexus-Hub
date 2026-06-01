import os, sys, json, traceback
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)

# ====== API 客户端 ======
deepseek_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1"
)

qwen_client = OpenAI(
    api_key=os.getenv("QWEN_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)

qnaigc_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://api.qnaigc.com/v1"
)

gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

claude_client = OpenAI(
    api_key=os.getenv("CLAUDE_API_KEY"),
    base_url="https://api.anthropic.com/v1"
)

zhipu_client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url="https://open.bigmodel.cn/api/paas/v4"
)

minimax_client = OpenAI(
    api_key=os.getenv("MINIMAX_API_KEY"),
    base_url="https://api.minimaxi.com/v1"
)

# ====== 角色 → 模型映射 ======
ROLE_MODELS = {
    "architect":      ("deepseek", "deepseek-v4-pro"),
    "writer":         ("deepseek", "deepseek-v4-flash"),
    "voter":          ("deepseek", "deepseek-v4-pro"),
    "backup_writer":  ("coze", "coze"),
    "math_expert":    ("qwen", "qwen-plus"),
    "code_expert":    ("claude", "claude-3-5-sonnet-20241022"),
    "security":       ("gemini", "gemini-2.5-flash"),
    "security_backup": ("deepseek", "deepseek-v4-pro"),
    "multimodal":     ("qnaigc", "llama-4-maverick"),
    "backup":         ("qnaigc", "mistral-large-3"),
    "zhipu_review":   ("zhipu", "glm-4.7-flash"),
    "minimax_review": ("minimax", "MiniMax-M2.7"),
}

# ====== 角色系统提示词 ======
SYSTEM_PROMPTS = {
    "architect": (
        "你是一名资深软件架构师。\n"
        "职责：分析需求，设计简洁的实现方案。\n"
        "规则：只输出方案描述（不超过300字），禁止输出代码。"
    ),
    "voter": (
        "你是一位技术评审专家，正在参与技术方案投票。\n"
        "请简要评估方案可行性，然后明确给出你的结论。\n\n"
        "重要：你的回复末尾必须明确写出「同意」或「反对」。"
    ),
    "writer": (
        "你是一名代码专家（TraeCN执行枢纽）。\n"
        "职责：根据架构方案编写完整的可运行代码。\n"
        "规则：\n"
        "1. 只输出纯 Python 代码，用简要注释说明关键逻辑。\n"
        "2. 绝对禁止使用 webbrowser、os.system、subprocess 等危险调用。\n"
        "3. 代码必须直接解决问题。\n"
        "4. 输出必须可直接运行。"
    ),
    "backup_writer": (
        "你是一名后备代码专家。\n"
        "职责：替代主写手完成任务。\n"
        "规则：同上，输出带注释的纯 Python 代码。"
    ),
    "math_expert": (
        "你是一名数学专家。\n"
        "职责：处理数学计算、算法优化相关任务。\n"
        "规则：只输出计算结果或代码，禁止多余解释。"
    ),
    "code_expert": (
        "你是一名高级代码专家。\n"
        "职责：审查代码质量、正确性、可维护性。\n"
        "规则：只输出 PASS 或 FAIL + 简短理由（不超过50字）。"
    ),
    "security": (
        "你是一名网络安全专家。\n"
        "职责：审查代码安全性，检测漏洞和风险。\n"
        "规则：只输出 PASS（安全）或 FAIL（不安全）+ 简短理由（不超过50字）。"
    ),
    "security_backup": (
        "你是一名网络安全专家（备用审计）。\n"
        "职责：审查代码安全性，重点检查明文密码、弱哈希、SQL注入、命令执行等。\n"
        "规则：只输出 PASS（安全）或 FAIL（不安全）+ 简短理由（不超过50字）。"
    ),
    "multimodal": (
        "你是一名多模态AI专家。\n"
        "职责：处理图像、音频、视频相关任务。\n"
        "规则：只输出代码或分析结果，禁止危险调用。"
    ),
    "backup": (
        "你是备用专家。\n"
        "职责：降级时提供支持。\n"
        "规则：按指令输出代码或分析结果。"
    ),
    "zhipu_review": (
        "你是一名技术评审专家。\n"
        "职责：在技术方案辩论中，从软件工程和实践角度提出评价意见。\n"
        "规则：输出简洁的技术评价，不超过200字。"
    ),
    "minimax_review": (
        "你是一名算法评审专家。\n"
        "职责：在技术方案辩论中，从算法效率和实现可行性角度提出评价意见。\n"
        "规则：输出简洁的技术评价，不超过200字。"
    ),
}

# ====== 角色提示词 ======
ROLE_TASK_PROMPTS = {
    "architect": "请为以下需求设计解决方案（只描述方案，不写代码）：\n",
    "voter": "",
    "writer": "根据架构方案编写Python代码（只输出代码）：\n",
    "backup_writer": "根据架构方案编写Python代码（只输出代码）：\n",
    "math_expert": "",
    "code_expert": "审查以下代码（只输出PASS/FAIL+理由）：\n",
    "security": "安全审查以下代码（只输出PASS/FAIL+理由）：\n",
    "security_backup": "安全审查以下代码（只输出PASS/FAIL+理由）：\n",
    "multimodal": "",
    "backup": "",
    "zhipu_review": "",
    "minimax_review": "",
}

def get_client(provider: str):
    if provider == "deepseek": return deepseek_client
    if provider == "qwen": return qwen_client
    if provider == "gemini": return gemini_client
    if provider == "claude": return claude_client
    if provider == "zhipu": return zhipu_client
    if provider == "minimax": return minimax_client
    return qnaigc_client

def _coze_chat(task: str, max_poll: int = 300, poll_interval: float = 1.0) -> str:
    import requests, time, uuid
    headers = {
        "Authorization": f"Bearer {os.getenv('COZE_API_KEY')}",
        "Content-Type": "application/json",
    }
    payload = {
        "bot_id": os.getenv("COZE_BOT_ID"),
        "user_id": f"nexus_{uuid.uuid4().hex[:8]}",
        "stream": False,
        "auto_save_history": True,
        "additional_messages": [{"role": "user", "content": task, "content_type": "text"}],
    }
    resp = requests.post("https://api.coze.cn/v3/chat", headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        return ""
    data = resp.json().get("data", {})
    chat_id = data.get("id")
    conversation_id = data.get("conversation_id")
    for _ in range(max_poll):
        time.sleep(poll_interval)
        r = requests.get(
            "https://api.coze.cn/v3/chat/retrieve",
            headers=headers,
            params={"chat_id": chat_id, "conversation_id": conversation_id},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("data", {}).get("status") == "completed":
            break
    r = requests.get(
        "https://api.coze.cn/v3/chat/message/list",
        headers=headers,
        params={"chat_id": chat_id, "conversation_id": conversation_id},
        timeout=10,
    )
    if r.status_code == 200:
        answers = [m.get("content", "") for m in r.json().get("data", []) if m.get("type") == "answer"]
        return "\n".join(answers)
    return ""

def call_coze(task: str) -> str:
    """调用扣子 API（写代码等长任务）"""
    try:
        return _coze_chat(task, max_poll=300, poll_interval=1.0)
    except Exception as e:
        print(f"[Coze] 失败: {e}")
    return ""

def call_coze_vote(task: str) -> str:
    """扣子投票：短轮询，只取 AGREE/DISAGREE"""
    try:
        short_task = task + "\n\n请只回复一个词：AGREE 或 DISAGREE。"
        text = _coze_chat(short_task, max_poll=25, poll_interval=0.8)
        if not text:
            return ""
        upper = text.upper()
        if "DISAGREE" in upper:
            return "DISAGREE"
        if "AGREE" in upper:
            return "AGREE"
        return text.strip()[:20]
    except Exception as e:
        print(f"[Coze投票] 失败: {e}")
    return ""

@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    task = data.get('task', '')
    role = data.get('role', 'writer')
    purpose = data.get('purpose', '')
    if not task:
        return jsonify({"error": "没有任务"}), 400

    provider, model = ROLE_MODELS.get(role, ("deepseek", "deepseek-v4-flash"))
    is_vote = purpose == "vote"
    is_review = purpose in ("review", "security")

    if provider == "coze":
        if is_vote:
            print(f"[{role}] Coze 投票...")
            result = call_coze_vote(task)
            if result:
                print(f"[{role}] Coze 投票: {result}")
                return jsonify({"code": result})
            print(f"[{role}] Coze 投票超时/失败")
            return jsonify({"error": "Coze vote timeout"}), 500
        print(f"[{role}] Coze 后备写手处理...")
        result = call_coze(task)
        if result:
            result = "\n".join(line.rstrip() for line in result.split("\n")).strip()
            preview = result[:60].replace('\n', '\\n')
            print(f"[{role}] Coze 生成成功，长度: {len(result)} | {preview}{'...' if len(result) > 60 else ''}")
            return jsonify({"code": result})
        print(f"[{role}] Coze 失败，降级到 DeepSeek-Flash")
        provider, model = "deepseek", "deepseek-v4-flash"

    client = get_client(provider)
    prompt_prefix = ROLE_TASK_PROMPTS.get(role, "") if not is_vote else ""
    full_task = (prompt_prefix + task) if not is_vote else task

    if is_vote:
        max_tokens, req_timeout, temperature = 1024, 120.0, 0.3
        system_content = SYSTEM_PROMPTS["voter"]
    elif is_review:
        max_tokens, req_timeout, temperature = 2000, 90.0, 0.1
        system_content = SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["writer"])
    else:
        max_tokens, req_timeout, temperature = 4000, 180.0, 0.2
        system_content = SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["writer"])

    print(f"[{role}/{purpose or 'default'}] {model} 收到任务: {task[:50]}...")
    try:
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": full_task},
        ]
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=req_timeout,
            )
        except Exception as timeout_err:
            print(f"[{role}] timeout参数可能不兼容，重试不带timeout... ({timeout_err})")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        raw = response.choices[0].message.content
        result = (raw or "").strip()
        if is_vote:
            if not result or ("同意" not in result and "反对" not in result):
                print(f"[{role}] 投票回复缺关键词，使用温度0重试... (当前: {result[:50] if result else '空'})")
                retry_msgs = [
                    {"role": "system", "content": "请直接回复一个词：「同意」或「反对」。不要任何其他内容。"},
                    {"role": "user", "content": full_task},
                ]
                response2 = client.chat.completions.create(
                    model=model,
                    messages=retry_msgs,
                    temperature=0,
                    max_tokens=16,
                )
                result = (response2.choices[0].message.content or "").strip()
        if result.startswith("```"):
            lines = result.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            result = "\n".join(lines)
        result = "\n".join(line.rstrip() for line in result.split("\n")).strip()
        preview = result[:60].replace('\n', '\\n')
        print(f"[{role}] {model} 生成成功，长度: {len(result)} | {preview}{'...' if len(result) > 60 else ''}")
        return jsonify({"code": result})
    except Exception as e:
        print(f"[{role}] {model} 生成失败: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("SOLO API 启动，端口 8765")
    print("角色配置：")
    for role, (prov, mdl) in ROLE_MODELS.items():
        print(f"  {role}: {prov}/{mdl}")
    app.run(host='127.0.0.1', port=8765, debug=False)