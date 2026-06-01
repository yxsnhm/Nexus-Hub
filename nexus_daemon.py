"""
智联枢纽 - 守护进程 (nexus_daemon.py)
完整辩论投票 + 三种执行模式

流程:
  辩论模式: 架构师设计 -> 7模型投票 -> 多数通过 -> 架构师下达编码指令
         -> SOLO写手编码 -> 架构师审核 -> Claude代码专家审核 -> Gemini安全审计 -> 保存
  直接执行: SOLO写手直接生成代码
  桌面自动化: SOLO写手生成自动化代码 -> 执行
"""

import os, sys, json, time, hmac, hashlib, threading
from pathlib import Path
import requests
from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).parent.resolve()
TASK_FILE = BASE_DIR / ".nexus_task.json"
RESULT_FILE = BASE_DIR / ".nexus_result.json"
PROGRESS_FILE = BASE_DIR / ".nexus_progress.json"
HEARTBEAT_FILE = BASE_DIR / ".daemon_heartbeat"
ABORT_FILE = BASE_DIR / ".nexus_abort"
OUTPUT_DIR = BASE_DIR / "output"
WEB_SERVER_PORT = 9000
SOLO_API = "http://127.0.0.1:8765/generate"
SECRET_KEY = os.getenv("NEXUS_SECRET_KEY", "nexus_default_secret").encode()

OUTPUT_DIR.mkdir(exist_ok=True)

# ====== 7个议员：各走各自 API（名称 → solo 角色 → 对外展示的 API 名） ======
VOTER_META = {
    "DeepSeek-Pro": {"role": "architect", "api": "DeepSeek v4-pro"},
    "TraeCN-SOLO": {"role": "writer", "api": "DeepSeek v4-flash"},
    "扣子": {"role": "backup_writer", "api": "Coze 扣子"},
    "千问": {"role": "math_expert", "api": "通义千问 qwen-plus"},
    "Claude": {"role": "code_expert", "api": "Claude"},
    "Gemini": {"role": "security", "api": "Gemini"},
    "Llama": {"role": "multimodal", "api": "Llama (qnaigc)"},
}

VOTER_LIST = list(VOTER_META.keys())

# 按用途设置 HTTP 超时（秒），连不上则快速跳过
SOLO_TIMEOUTS = {
    "vote": 120,
    "review": 55,
    "security": 55,
    "code": 180,
    "default": 120,
}

# 主代码写手（与 solo_api.py 中 writer 角色一致）
CODE_WRITER = {
    "writer_name": "SOLO写手",
    "writer_alias": "TraeCN执行枢纽",
    "writer_role": "writer",
    "writer_model": "deepseek-v4-flash",
    "writer_display": "SOLO写手 (deepseek-v4-flash)",
}

def verify_signature(task_data: dict, signature: str) -> bool:
    payload = json.dumps(task_data, sort_keys=True).encode()
    expected = hmac.new(SECRET_KEY, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def update_heartbeat():
    with open(HEARTBEAT_FILE, 'w') as f:
        f.write(str(time.time()))

def _save_progress(data: dict):
    if _task_aborted:
        return
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _ws_push(data)

_ws_log_cursor = 0  # WebSocket 增量推送游标

def _ws_push(data: dict):
    """推送增量进度到前端（非阻塞），只发新增日志，不发全量历史"""
    global _ws_log_cursor
    try:
        logs = data.get("logs", [])
        delta = logs[_ws_log_cursor:]
        _ws_log_cursor = len(logs)
        push = {
            "status": data.get("status", "running"),
            "mode": data.get("mode", ""),
            "step": data.get("step", 0),
            "step_total": data.get("step_total", 9),
            "step_label": data.get("step_label", ""),
            "votes": data.get("votes", {}),
            "vote_summary": data.get("vote_summary", ""),
            "plan_preview": data.get("plan_preview", ""),
            "result": data.get("result", ""),
            "logs": delta,
        }
        requests.post(f"http://127.0.0.1:{WEB_SERVER_PORT}/api/progress_push",
                      json=push, timeout=1)
    except Exception:
        pass

def _load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {}
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def init_progress(mode: str, task_desc: str, submitted_at: float):
    global _ws_log_cursor
    _ws_log_cursor = 0  # 新任务重置 WebSocket 游标
    data = {
        "submitted_at": submitted_at,
        "status": "running",
        "mode": mode,
        "task": task_desc[:200],
        "step": 0,
        "step_total": 9 if mode == "debate" else (4 if mode == "direct" else 5),
        "step_label": "准备中",
        "votes": {},
        "vote_summary": "",
        "plan_preview": "",
        "logs": [],
    }
    _save_progress(data)

def progress_log(msg: str, level: str = "info"):
    data = _load_progress()
    if not data:
        return
    logs = data.setdefault("logs", [])
    logs.append({"ts": time.time(), "level": level, "msg": msg})
    if len(logs) > 300:
        data["logs"] = logs[-300:]
    _save_progress(data)

def progress_step(step: int, label: str, step_total: int = None):
    data = _load_progress()
    if not data:
        return
    data["step"] = step
    data["step_label"] = label
    if step_total:
        data["step_total"] = step_total
    _save_progress(data)
    progress_log(f"[{step}/{data.get('step_total', 9)}] {label}", "highlight")

def progress_votes(votes: dict, agree_count: int = None):
    data = _load_progress()
    if not data:
        return
    data["votes"] = votes
    if agree_count is None:
        agree_count = sum(1 for v in votes.values() if v == "同意")
    data["vote_summary"] = f"{agree_count}/{len(votes)} 赞成"
    _save_progress(data)

def progress_plan(plan: str):
    data = _load_progress()
    if not data:
        return
    data["plan_preview"] = plan[:600]
    _save_progress(data)

def finish_progress(success: bool = True):
    data = _load_progress()
    if not data:
        return
    msg = "辩论完成" if success and data.get("mode") == "debate" else ("任务完成" if success else "任务失败")
    data.setdefault("logs", []).append({
        "ts": time.time(),
        "level": "success" if success else "error",
        "msg": "✅ " + msg if success else "❌ " + msg,
    })
    data["status"] = "completed" if success else "failed"
    _save_progress(data)

def call_solo(task: str, role: str = "writer", purpose: str = "default") -> str:
    """调用 SOLO API；purpose: vote / review / security / code"""
    timeout = SOLO_TIMEOUTS.get(purpose, SOLO_TIMEOUTS["default"])
    try:
        resp = requests.post(
            SOLO_API,
            json={"task": task, "role": role, "purpose": purpose},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            code = data.get("code", "")
            if code.startswith("```"):
                code = code.split("\n", 1)[1].rsplit("\n", 1)[0]
            return code.strip()
        err = ""
        try:
            err = resp.json().get("error", resp.text[:120])
        except Exception:
            err = resp.text[:120]
        print(f"  [SOLO/{role}] {purpose} 错误 {resp.status_code}: {err}")
    except requests.exceptions.Timeout:
        print(f"  [SOLO/{role}] {purpose} 超时({timeout}s)，跳过")
    except Exception as e:
        print(f"  [SOLO/{role}] {purpose} 失败: {e}")
    return ""

def parse_vote_answer(result: str) -> str:
    if not result:
        return "SKIP"
    upper = result.upper().strip()[:30]
    if "DISAGREE" in upper or (upper.startswith("NO") and "AGREE" not in upper) or "反对" in result:
        return "DISAGREE"
    if "AGREE" in upper or "赞成" in result:
        return "AGREE"
    return "SKIP"

def save_code_file(code: str) -> Path:
    timestamp = int(time.time())
    filepath = OUTPUT_DIR / f"task_{timestamp}.py"
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(code)
    print(f"  代码已保存: {filepath.name}")
    return filepath

def write_failure(message: str):
    progress_log(f"❌ {message}", "error")
    finish_progress(False)
    with open(RESULT_FILE, 'w', encoding='utf-8') as f:
        json.dump({"status": "failed", "message": message, "code": "", "file_path": "", "timestamp": time.time()}, f, ensure_ascii=False)

def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def is_review_failed(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    return "FAIL" in upper or "REJECT" in upper

def is_final_approved(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    if "REJECT" in upper:
        return False
    return "APPROVED" in upper or ("PASS" in upper and "FAIL" not in upper)

def is_security_passed(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    if "FAIL" in upper or "UNSAFE" in upper or "不安全" in text:
        return False
    return "PASS" in upper or "安全" in text

def run_security_audit(code: str, task_desc: str) -> tuple:
    """多通道安全审计：先快后慢，超时即跳过。返回 (是否通过, 说明)"""
    auditors = [
        ("DeepSeek", "security_backup"),
        ("Gemini", "security"),
    ]
    prompt = (
        f"审查以下代码是否存在安全漏洞（明文密码、弱哈希、注入、危险系统调用等）。\n"
        f"只输出 PASS（安全）或 FAIL（不安全）+ 简短理由（不超过50字）。\n\n"
        f"需求：{task_desc}\n\n代码：\n{code[:1200]}"
    )
    last_note = ""
    for label, role in auditors:
        review = call_solo(prompt, role=role, purpose="security")
        if is_security_passed(review):
            return True, f"{label}: {review[:80]}"
        if review:
            last_note = f"{label}: {review[:80]}"
            if is_review_failed(review):
                return False, last_note
        else:
            last_note = f"{label}: API不可用"
    if last_note:
        return False, last_note
    return False, "所有安全审核渠道均无响应"

# ====== 辩论模式（使用 DebateStateMachine） ======
def run_debate_mode(task_desc: str):
    from debate_state_machine import DebateStateMachine
    from nexus_hub_integration import NexusDebateRunner
    print_header("辩论模式")
    progress_log("======== 辩论模式 ========", "highlight")

    def on_step(step, label, total):
        progress_step(step, label, total)

    def on_log(msg, level):
        progress_log(msg, level)

    sm = DebateStateMachine(on_step=on_step, on_log=on_log, on_vote=progress_votes)

    # === 接入 NexusHub 消息总线 ===
    try:
        runner = NexusDebateRunner(sm)
        runner.register_bridge("architect", "架构师", "deepseek", "deepseek-v4-pro",
                               ["系统设计", "安全审计", "代码审核"])
        runner.register_bridge("writer", "主写手", "deepseek", "deepseek-v4-flash",
                               ["代码生成", "快速迭代"])
        runner.register_bridge("zhipu_review", "智谱评审", "zhipu", "glm-4.7-flash",
                               ["技术评审", "算法验证"])
        runner.register_bridge("math_expert", "数学专家", "qwen", "qwen-plus",
                               ["数学推理", "算法设计"])
        runner.register_bridge("minimax_review", "MiniMax评审", "minimax", "MiniMax-M2.7",
                               ["方案评估"])
        runner.start_session()
        progress_log(" NexusHub 已启动，议员已注册", "info")
    except Exception as e:
        progress_log(f" NexusHub 启动失败（不影响主流程）: {e}", "warning")
        runner = None

    result = sm.run(task_desc)

    # === 输出 Hub 会话摘要 ===
    if runner:
        try:
            runner.end_session()
            summary = runner.get_summary()
            hub_status = runner.status()
            progress_log(f" Hub: {len(summary)}条消息, {hub_status.get('online_councilors',0)}在线", "info")
        except Exception:
            pass

    if result is None:
        write_failure("辩论模式未通过审核，未保存代码")
        return None

    code, filepath = result
    print(f"\n  辩论完成，代码已保存: {filepath.name}")

    progress_votes(sm.get_votes(), sum(1 for v in sm.get_votes().values() if v == "同意"))
    print_header("辩论完成")
    finish_progress(True)
    return code, filepath

# ====== 直接执行模式（含简化审核） ======
def run_direct_mode(task_desc: str):
    print_header("直接执行模式")
    progress_log("======== 直接执行模式 ========", "highlight")
    progress_step(1, "生成代码", 4)
    print(f"  任务: {task_desc}")
    if "扣子写" in task_desc or "扣子" in task_desc:
        print("  使用扣子写手...")
        from coze_bridge import CozeBridge
        cb = CozeBridge()
        code = cb.generate_code(task_desc)
        writer_info = {"writer_name": "扣子写手", "writer_alias": "Coze", "writer_role": "backup_writer", "writer_model": "coze", "writer_display": "扣子写手 (Coze)", "coder": "扣子写手 (Coze)"}
    else:
        code = call_solo(task_desc, role="writer", purpose="code")
        writer_info = {"writer_name": "SOLO写手", "writer_alias": "TraeCN执行枢纽", "writer_role": "writer", "writer_model": "deepseek-v4-flash", "writer_display": "SOLO写手 (deepseek-v4-flash)", "coder": "SOLO写手 (deepseek-v4-flash)"}
    if not code:
        write_failure("写手未能返回代码")
        return None
    print(f"  生成代码长度: {len(code)} 字符")
    progress_log(f"💻 代码已生成 ({len(code)} 字符)", "info")

    # 步骤2: 快速架构师审核
    progress_step(2, "架构师审核", 4)
    for attempt in range(2):
        review = call_solo(
            f"你是资深架构师，审核以下代码是否满足需求。只输出 PASS 或 FAIL + 简短理由。\n\n需求：{task_desc}\n\n代码：\n{code[:800]}",
            role="architect", purpose="review")
        if review and "PASS" in review.upper() and "FAIL" not in review.upper():
            print(f"  架构师审核: 通过")
            progress_log("🔍 架构师审核: 通过", "success")
            break
        reason = (review or "无响应")[:80]
        print(f"  架构师审核未通过: {reason}")
        progress_log(f"🔍 架构师审核未通过: {reason}", "waiting")
        code = call_solo(
            f"根据审核意见修改代码。\n意见：{review}\n原始需求：{task_desc}",
            role="writer", purpose="code")
        if not code:
            write_failure("审核未通过，修改代码失败")
            return None
        print(f"  修改完成，长度: {len(code)} 字符")
    else:
        write_failure("架构师审核未通过，已重试仍不合格")
        return None

    # 步骤3: 快速安全审计
    progress_step(3, "安全审计", 4)
    for attempt in range(2):
        audit = call_solo(
            f"审查以下代码是否存在安全漏洞。只输出 PASS 或 FAIL + 简短理由。\n\n需求：{task_desc}\n\n代码：\n{code[:1200]}",
            role="architect", purpose="security")
        if audit and "PASS" in audit.upper() and "FAIL" not in audit.upper():
            print(f"  安全审计: 通过")
            progress_log("🔒 安全审计: 通过", "success")
            break
        reason = (audit or "无响应")[:80]
        print(f"  安全审计未通过: {reason}")
        progress_log(f"🔒 安全审计未通过: {reason}", "error")
        code = call_solo(
            f"修复安全问题后重新输出完整代码。\n安全意见：{audit}\n原始需求：{task_desc}",
            role="writer", purpose="code")
        if not code:
            write_failure("安全审计未通过，修复失败")
            return None
        print(f"  修复完成，长度: {len(code)} 字符")
    else:
        write_failure("安全审计未通过")
        return None

    # 步骤4: 保存
    progress_step(4, "保存代码文件", 4)
    filepath = save_code_file(code)
    print(f"  代码已保存: {filepath.name}")
    progress_log(f"📁 已保存（审核通过）: {filepath.name}", "success")
    print_header("任务完成")
    finish_progress(True)
    return code, filepath, writer_info

# ====== 桌面自动化模式 ======
def run_desktop_mode(task_desc: str):
    print_header("桌面自动化模式")
    progress_log("======== 桌面自动化模式 ========", "highlight")
    progress_step(1, "分析任务意图", 5)
    print(f"  任务: {task_desc}")

    progress_step(2, "生成自动化脚本", 5)
    code_prompt = (
        f"你是桌面自动化专家。请编写Python自动化脚本完成以下任务。\n"
        f"规则：\n"
        f"1. 使用 pyautogui 库进行鼠标键盘操作\n"
        f"2. 可以使用 pyperclip 进行剪贴板操作\n"
        f"3. 可以用 os.system('notepad') 或 os.system('start ...') 打开程序\n"
        f"4. 禁止使用 os.remove、os.unlink、shutil.rmtree 等删除操作\n"
        f"5. 禁止使用 subprocess 执行任意命令\n"
        f"6. 禁止使用 socket 等网络模块\n"
        f"7. 只输出纯Python代码，不要任何解释\n\n"
        f"任务：{task_desc}"
    )
    code = call_solo(code_prompt, role="writer", purpose="code")
    if not code:
        progress_log("⚠️ 主写手生成失败，尝试备用模型...", "waiting")
        code = call_solo(task_desc, role="multimodal", purpose="code")
    if not code:
        write_failure("桌面自动化代码生成失败")
        return None
    progress_log(f"💻 自动化脚本已生成 ({len(code)} 字符)", "info")

    progress_step(3, "安全审查", 5)
    import re
    dangerous_patterns = [
        (r"os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree", "删除文件/目录操作"),
        (r"subprocess\.(call|Popen|run)\(.*shell\s*=\s*True", "Shell命令执行"),
        (r"(import|from)\s+(socket|http\.server|ftplib|telnetlib)", "网络服务模块"),
        (r"__import__\s*\(|exec\s*\(|eval\s*\(|compile\s*\(", "动态代码执行"),
    ]
    allowed_risks = [
        (r"os\.system\(.*notepad", "打开记事本"),
        (r"os\.system\(.*start\s+", "打开程序"),
    ]
    warnings = []
    for pattern, desc in dangerous_patterns:
        if re.search(pattern, code):
            warnings.append(f"⚠️ 检测到{desc}")
    if warnings:
        print("  ⚠️ 安全扫描发现风险：")
        for w in warnings:
            print(f"    {w}")
        print("  ℹ️  脚本已保存但不会自动执行，请手动审核后运行")
        progress_log(f"⚠️ 安全风险，脚本已保存需手动审核", "error")
        filepath = save_code_file(code)
        progress_step(5, "完成（需手动审核）", 5)
        progress_log(f"📄 脚本已保存: {filepath.name}", "info")
        finish_progress(True)
        return code, filepath
    progress_log("🔒 安全审查: 通过", "success")

    progress_step(4, "执行自动化", 5)
    print("  执行自动化脚本...")
    exec_ok = False
    run_log = []
    try:
        from desktop_automator import DesktopAutomator
        automator = DesktopAutomator(safety_mode=True)
        automator.take_snapshot()

        import subprocess
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=20,
                cwd=str(BASE_DIR),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}
            )
            exec_ok = result.returncode == 0
            if result.stdout:
                stdout_text = result.stdout.strip()[:500]
                run_log.append(f"STDOUT: {stdout_text}")
                print(f"  输出: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.strip()[:500]
                run_log.append(f"STDERR: {stderr_text}")
                print(f"  错误: {stderr_text}")
        except subprocess.TimeoutExpired:
            exec_ok = True
            run_log = ["桌面操作已在后台执行（GUI进程未返回，这是正常的）"]
            progress_log("🖥️ 桌面操作已在后台执行", "info")

        screenshot_path = None
        try:
            screenshot_path = automator.screenshot(f"desktop_task_{int(time.time())}.png")
        except Exception:
            pass

        automator.audit.save_to_file(str(BASE_DIR / f"desktop_audit_{int(time.time())}.json"))

        if exec_ok:
            progress_log("✅ 自动化脚本执行完成", "success")
        else:
            progress_log(f"⚠️ 脚本执行返回码: {result.returncode if 'result' in dir() else 'timeout'}", "warning")

        if screenshot_path:
            progress_log(f"📸 截图已保存: {screenshot_path}", "info")

    except Exception as e:
        run_log = [f"执行异常: {e}"]
        progress_log(f"❌ 执行异常: {e}", "error")
        exec_ok = False

    progress_step(5, "保存结果", 5)
    filepath = save_code_file(code)
    progress_log(f"📄 脚本已保存: {filepath.name}", "info")
    print_header("桌面自动化完成")
    finish_progress(True)

    return code, filepath

# ====== 任务模式识别 ======
def identify_mode(task_desc: str) -> str:
    if "直接执行" in task_desc or task_desc.startswith("[直接]"):
        return "direct"
    if task_desc.startswith("[辩论]"):
        return "debate"
    if task_desc.startswith("[桌面]"):
        return "desktop"
    desktop_kw = ["打开", "点击", "输入", "键盘", "鼠标", "记事本", "浏览器", "桌面", "窗口", "粘贴", "按键", "截图", "自动化", "搜索"]
    coding_kw = ["写", "开发", "函数", "类", "模块", "实现", "设计", "数据库", "登录", "API", "算法", "爬虫", "接口", "架构", "重构", "代码", "编程", "脚本"]
    is_desktop = any(k in task_desc for k in desktop_kw)
    is_coding = any(k in task_desc for k in coding_kw)
    if is_desktop and not is_coding:
        return "desktop"
    return "debate"

def resolve_mode(task_data: dict) -> str:
    """优先使用 Web 提交时指定的 execution_mode"""
    explicit = (task_data.get("mode") or task_data.get("execution_mode") or "").strip().lower()
    if explicit in ("debate", "direct", "desktop"):
        return explicit
    return identify_mode(task_data.get("content", ""))

TASK_TIMEOUT = 900  # 15分钟超时
_task_timeout_flag = False
_task_aborted = False

def _run_task_with_timeout(task_data: dict):
    global _task_timeout_flag, _task_aborted
    _task_timeout_flag = False
    _task_aborted = False
    result_container = {"result": None, "error": None}

    def _worker():
        try:
            result_container["result"] = _process_task_inner(task_data)
        except Exception as e:
            result_container["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    remaining = TASK_TIMEOUT
    while remaining > 0:
        chunk = min(30, remaining)
        t.join(chunk)
        remaining -= chunk
        if not t.is_alive():
            break
        update_heartbeat()
        if ABORT_FILE.exists():
            print(f"\n🛑 收到终止信号，正在中止任务...", flush=True)
            progress_log("🛑 收到终止信号，正在中止任务...", "error")
            _task_aborted = True
            ABORT_FILE.unlink()
            write_failure("用户终止了任务")
            return None
    if t.is_alive():
        _task_timeout_flag = True
        print(f"\n⚠️ 任务超时 ({TASK_TIMEOUT}s)，强制终止", flush=True)
        progress_log(f"⚠️ 任务超时 ({TASK_TIMEOUT}s)，强制终止", "error")
        write_failure(f"任务超时 ({TASK_TIMEOUT}s)")
        return None
    if result_container["error"]:
        raise RuntimeError(result_container["error"])
    return result_container["result"]

def _process_task_inner(task_data: dict):
    task_desc = task_data.get("content", "")
    mode = resolve_mode(task_data)
    submitted_at = float(task_data.get("timestamp", time.time()))
    init_progress(mode, task_desc, submitted_at)
    print(f"\n任务: {task_desc}")
    print(f"模式: {mode}")
    progress_log(f"📋 任务: {task_desc}", "highlight")
    progress_log(f"⚙️ 模式: {mode}", "info")

    if mode == "direct":
        result = run_direct_mode(task_desc)
    elif mode == "desktop":
        result = run_desktop_mode(task_desc)
    else:
        result = run_debate_mode(task_desc)

    if result is None:
        if not RESULT_FILE.exists():
            write_failure("任务执行失败")
        return

    if len(result) == 3:
        code, filepath, writer_info = result
    else:
        code, filepath = result
        writer_info = {"writer_name": "SOLO写手", "writer_alias": "TraeCN执行枢纽", "writer_role": "writer", "writer_model": "deepseek-v4-flash", "writer_display": "SOLO写手 (deepseek-v4-flash)", "coder": "SOLO写手 (deepseek-v4-flash)"}


    result_data = {
        "status": "completed",
        "code": code,
        "file_path": str(filepath),
        "output_file": filepath.name,
        "lines": code.count('\n') + 1,
        "timestamp": time.time(),
        "submitted_at": submitted_at,
        **writer_info,
        "coder": writer_info.get("coder", writer_info.get("writer_display")),

    }
    with open(RESULT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False)
    finish_progress(True)
    print(f"\n结果已写入 .nexus_result.json")

# ====== 清理旧文件 ======
def cleanup_old_files():
    try:
        now = time.time()
        for f in OUTPUT_DIR.glob("*.py"):
            if now - f.stat().st_mtime > 600:
                f.unlink()
                print(f"  自动清理: {f.name}")
    except:
        pass

# ====== 主循环 ======
def main_loop():
    print_header("智联枢纽 守护进程")
    print(f"  监控文件: {TASK_FILE.name}")
    print(f"  输出目录: {OUTPUT_DIR.name}")
    print(f"  SOLO API: {SOLO_API}")
    print(f"  投票模型: {', '.join(VOTER_LIST)}")
    loop_count = 0
    while True:
        update_heartbeat()
        loop_count += 1
        if loop_count % 30 == 0:
            cleanup_old_files()
        try:
            if not TASK_FILE.exists():
                time.sleep(2)
                continue
            with open(TASK_FILE, 'r', encoding='utf-8') as f:
                wrapper = json.load(f)
            task_data = wrapper.get("task", {})
            signature = wrapper.get("signature", "")
            if signature and not verify_signature(task_data, signature):
                time.sleep(2)
                continue
            if task_data.get('status') == 'new':
                _run_task_with_timeout(task_data)
                if TASK_FILE.exists():
                    TASK_FILE.unlink()
                    print("  任务文件已清理，等待新任务...")
        except Exception as e:
            print(f"  轮询异常: {e}")
        time.sleep(2)

if __name__ == "__main__":
    if TASK_FILE.exists(): TASK_FILE.unlink()
    if HEARTBEAT_FILE.exists(): HEARTBEAT_FILE.unlink()
    if ABORT_FILE.exists(): ABORT_FILE.unlink()
    while True:
        try:
            main_loop()
        except KeyboardInterrupt:
            print("\n守护进程已停止", flush=True)
            break
        except Exception as e:
            print(f"\n❌ 守护进程异常退出: {e}，5秒后自动重启...", flush=True)
            time.sleep(5)
