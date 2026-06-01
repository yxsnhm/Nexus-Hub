"""
智联枢纽 - Web可视化界面 (用户认证版)
"""
import os, sys, json, time, hmac, hashlib, uuid, re, asyncio
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding='utf-8')

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
from group_chat import chat_manager, stream_ai_response

app = FastAPI(title="智联枢纽 Web 控制台", version="6.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", os.urandom(24).hex()))

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"❌ 全局异常: {exc}", flush=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "服务器内部错误"}
    )

# === WebSocket 实时推送 ===
ws_clients: list = []
ws_lock = asyncio.Lock()

async def ws_broadcast(data: dict):
    """广播进度到所有 WebSocket 客户端"""
    async with ws_lock:
        dead = []
        for ws in ws_clients[:]:
            try:
                await asyncio.wait_for(ws.send_json(data), timeout=5)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                ws_clients.remove(ws)
            except ValueError:
                pass

PROJECT_DIR = Path(__file__).parent.resolve()
TASK_FILE = PROJECT_DIR / ".nexus_task.json"
RESULT_FILE = PROJECT_DIR / ".nexus_result.json"
PENDING_FILE = PROJECT_DIR / ".nexus_pending.json"
PROGRESS_FILE = PROJECT_DIR / ".nexus_progress.json"
HEARTBEAT_FILE = PROJECT_DIR / ".daemon_heartbeat"
OUTPUT_DIR = PROJECT_DIR / "output"
SECRET_KEY = os.getenv("NEXUS_SECRET_KEY", "nexus_default_secret").encode()
USER_DB = PROJECT_DIR / "users.db"

def sign_task(task_data: dict) -> str:
    payload = json.dumps(task_data, sort_keys=True).encode()
    return hmac.new(SECRET_KEY, payload, hashlib.sha256).hexdigest()

# ==============================================================================
# 用户数据库初始化
# ==============================================================================
import sqlite3

def init_user_db():
    with sqlite3.connect(str(USER_DB)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                reset_code TEXT,
                reset_expire REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_user_db()

# ==============================================================================
# 用户认证函数
# ==============================================================================
from werkzeug.security import generate_password_hash, check_password_hash

def get_user_by_email(email):
    with sqlite3.connect(str(USER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        return dict(user) if user else None

def create_user(email, password):
    password_hash = generate_password_hash(password[:128])
    try:
        with sqlite3.connect(str(USER_DB)) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
            conn.commit()
            return True, None
    except sqlite3.IntegrityError:
        return False, "邮箱已被注册"
    except Exception as e:
        return False, str(e)

def verify_user(email, password):
    user = get_user_by_email(email)
    if not user:
        return False
    return check_password_hash(user['password_hash'], password)

def set_reset_code(email):
    user = get_user_by_email(email)
    if not user:
        return None
    code = str(uuid.uuid4())[:8].upper()
    expire = time.time() + 3600
    with sqlite3.connect(str(USER_DB)) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET reset_code = ?, reset_expire = ? WHERE email = ?", (code, expire, email))
        conn.commit()
    return code

def verify_reset_code(email, code):
    user = get_user_by_email(email)
    if not user:
        return False, "用户不存在"
    if user.get('reset_code') != code:
        return False, "重置码错误"
    if time.time() > (user.get('reset_expire') or 0):
        return False, "重置码已过期"
    return True, None

def reset_password(email, code, new_password):
    ok, msg = verify_reset_code(email, code)
    if not ok:
        return False, msg
    password_hash = generate_password_hash(new_password[:128])
    with sqlite3.connect(str(USER_DB)) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password_hash = ?, reset_code = NULL, reset_expire = NULL WHERE email = ?", (password_hash, email))
        conn.commit()
    return True, None

def validate_email(email):
    return re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email) is not None

# ==============================================================================
# 任务提交函数
# ==============================================================================
def write_local_task(task_desc: str, priority: str = "P2", mode: str = "debate", user_id: int = None, user_email: str = "") -> float:
    # 清理旧输出文件，防止 get_latest_output() 捡到旧数据
    if OUTPUT_DIR.is_dir():
        for f in OUTPUT_DIR.glob("*.py"):
            try:
                f.unlink()
            except Exception:
                pass
    submitted_at = time.time()
    task_data = {
        'type': 'coding',
        'content': task_desc,
        'priority': priority,
        'mode': mode,
        'execution_mode': mode,
        'from': 'web_interface',
        'user_id': user_id,
        'user_email': user_email,
        'timestamp': submitted_at,
        'status': 'new'
    }
    signature = sign_task(task_data)
    with open(TASK_FILE, 'w', encoding='utf-8') as f:
        json.dump({"task": task_data, "signature": signature}, f, ensure_ascii=False, indent=2)
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump({"submitted_at": submitted_at, "mode": mode}, f, ensure_ascii=False)
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "submitted_at": submitted_at,
            "status": "pending",
            "mode": mode,
            "step": 0,
            "step_total": 9 if mode == "debate" else (4 if mode == "direct" else 5),
            "step_label": "等待守护进程",
            "logs": [{"ts": submitted_at, "level": "waiting", "msg": "⏳ 任务已提交，等待守护进程拾取..."}],
            "votes": {},
            "vote_summary": "",
            "plan_preview": "",
        }, f, ensure_ascii=False)
    return submitted_at

def check_daemon_status() -> dict:
    if HEARTBEAT_FILE.exists():
        with open(HEARTBEAT_FILE, 'r') as f:
            last_beat = float(f.read().strip())
        ago = time.time() - last_beat
        if ago < 60:
            return {"running": True, "last_heartbeat": f"{ago:.0f}秒前"}
        else:
            return {"running": False, "reason": f"心跳超时({ago:.0f}秒前)"}
    return {"running": False, "reason": "心跳文件不存在"}

DEFAULT_WRITER_DISPLAY = "SOLO写手 (deepseek-v4-flash)"

def build_success_payload(code: str, file_path: str = "", meta: dict = None) -> dict:
    meta = meta or {}
    writer_display = meta.get("writer_display") or DEFAULT_WRITER_DISPLAY
    fp = (file_path or meta.get("file_path", "")).replace("\\", "/")
    output_file = meta.get("output_file") or (Path(fp).name if fp else "")
    return {
        "completed": True,
        "failed": False,
        "writer_name": meta.get("writer_name", "SOLO写手"),
        "writer_alias": meta.get("writer_alias", "TraeCN执行枢纽"),
        "writer_model": meta.get("writer_model", "deepseek-v4-flash"),
        "writer_display": writer_display,
        "coder": writer_display,
        "output_file": output_file,
        "code_length": len(code),
        "code_preview": code[:500] + ("..." if len(code) > 500 else ""),
        "file_path": fp,
    }

def get_latest_output(since: float = 0) -> dict:
    if not OUTPUT_DIR.is_dir():
        return None
    py_files = [f for f in OUTPUT_DIR.glob("*.py") if f.stat().st_mtime >= since - 0.5]
    if not py_files:
        return None
    latest = max(py_files, key=os.path.getmtime)
    try:
        with open(latest, 'r', encoding='utf-8') as f:
            code = f.read()
        return {
            "status": "completed",
            "code": code,
            "writer_display": DEFAULT_WRITER_DISPLAY,
            "coder": DEFAULT_WRITER_DISPLAY,
            "output_file": latest.name,
            "timestamp": os.path.getmtime(latest),
            "file_path": str(latest).replace("\\", "/"),
        }
    except:
        return None

# ==============================================================================
# 用户认证路由
# ==============================================================================

@app.get("/user/status")
async def user_status(request: Request):
    session = request.session
    if 'user_id' in session:
        return JSONResponse({
            "logged_in": True,
            "user_id": session.get('user_id'),
            "email": session.get('email', '')
        })
    return JSONResponse({"logged_in": False})

@app.post("/user/register")
async def user_register(request: Request):
    data = await request.json()
    email = data.get('email', '').strip()
    password = data.get('password', '')

    if not validate_email(email):
        return JSONResponse({"success": False, "error": "邮箱格式不正确"})
    if len(password) < 8:
        return JSONResponse({"success": False, "error": "密码至少8位"})

    ok, msg = create_user(email, password)
    if ok:
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": msg})

@app.post("/user/login")
async def user_login(request: Request):
    data = await request.json()
    email = data.get('email', '').strip()
    password = data.get('password', '')

    if verify_user(email, password):
        user = get_user_by_email(email)
        request.session['user_id'] = user['id']
        request.session['email'] = email
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "邮箱或密码错误"})

@app.get("/user/logout")
async def user_logout(request: Request):
    request.session.clear()
    return JSONResponse({"success": True})

@app.post("/user/forgot-password")
async def forgot_password(request: Request):
    data = await request.json()
    email = data.get('email', '').strip()

    if not validate_email(email):
        return JSONResponse({"success": False, "error": "邮箱格式不正确"})

    user = get_user_by_email(email)
    if not user:
        return JSONResponse({"success": True, "message": "如果邮箱存在，重置码已生成"})

    code = set_reset_code(email)
    return JSONResponse({
        "success": True,
        "message": f"重置码: {code}（1小时内有效）",
        "reset_code": code  # 开发环境下直接返回，正式环境应发送邮件
    })

@app.post("/user/reset-password")
async def reset_password_api(request: Request):
    data = await request.json()
    email = data.get('email', '').strip()
    code = data.get('reset_code', '').strip()
    new_password = data.get('new_password', '')

    if not validate_email(email):
        return JSONResponse({"success": False, "error": "邮箱格式不正确"})
    if not code:
        return JSONResponse({"success": False, "error": "请输入重置码"})
    if len(new_password) < 8:
        return JSONResponse({"success": False, "error": "新密码至少8位"})

    ok, msg = reset_password(email, code, new_password)
    if ok:
        return JSONResponse({"success": True, "message": "密码重置成功"})
    return JSONResponse({"success": False, "error": msg})

# ==============================================================================
# 任务提交路由（需要登录验证）
# ==============================================================================

@app.post("/api/submit_task")
async def submit_task(request: Request):
    session = request.session
    user_id = session.get('user_id') or 0
    user_email = session.get('email', '') or 'local@nexus'

    data = await request.json()
    task_desc = data.get("task", "").strip() or data.get("task_desc", "").strip()
    if not task_desc:
        return JSONResponse({"status": "error", "message": "任务描述不能为空"})

    status = check_daemon_status()
    if not status["running"]:
        return JSONResponse({"status": "error", "message": f"守护进程未运行({status.get('reason', '未知')})，请先启动守护进程"})

    mode = (data.get("mode") or "debate").strip().lower()
    if mode not in ("debate", "direct", "desktop"):
        mode = "debate"

    if TASK_FILE.exists():
        TASK_FILE.unlink()
    if RESULT_FILE.exists():
        RESULT_FILE.unlink()
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
    ABORT_FILE = PROJECT_DIR / ".nexus_abort"
    if ABORT_FILE.exists():
        ABORT_FILE.unlink()
    submitted_at = write_local_task(task_desc, "P0", mode, user_id, user_email)
    print(f"📤 任务已写入 [用户={user_email}, 模式={mode}]", flush=True)

    return JSONResponse({
        "status": "started",
        "message": "任务已提交",
        "mode": mode,
        "submitted_at": submitted_at,
        "daemon_status": "可用"
    })

# ==============================================================================
# WebSocket 实时进度推送
# ==============================================================================

@app.websocket("/ws/progress")
async def ws_progress(websocket: WebSocket):
    await websocket.accept()
    async with ws_lock:
        ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with ws_lock:
            if websocket in ws_clients:
                ws_clients.remove(websocket)

@app.post("/api/progress_push")
async def progress_push(request: Request):
    """daemon 推送进度数据，通过 WebSocket 广播到前端"""
    try:
        data = await request.json()
        await ws_broadcast(data)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

# ==============================================================================
# AI 实时群聊 WebSocket（多房间）
# ==============================================================================

@app.websocket("/ws/chat/{room_id}")
async def ws_chat(websocket: WebSocket, room_id: str):
    await websocket.accept()
    room = chat_manager.get_room(room_id)
    if not room:
        try:
            await websocket.send_json({"type": "error", "text": "房间不存在"})
        except Exception:
            pass
        return

    room.add_client(websocket)

    try:
        await websocket.send_json({
            "type": "participants",
            "room_id": room_id,
            "room_name": room.name,
            "participants": [
                chat_manager.get_ai(pid) for pid in room.participants
                if chat_manager.get_ai(pid)
            ]
        })
        history_msgs = room.history.get_history()
        if history_msgs:
            await websocket.send_json({
                "type": "history",
                "messages": history_msgs
            })
    except Exception:
        room.remove_client(websocket)
        return

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                await room.broadcast({
                    "type": "message",
                    "from": "user",
                    "name": "我",
                    "text": user_text,
                })

                room.history.add_message("user", user_text, "用户")
                history = room.history.get_history()

                relevant_participants = [
                    chat_manager.get_ai(pid) for pid in room.participants
                    if chat_manager.get_ai(pid)
                ]

                tasks = []
                for p in relevant_participants:
                    task = asyncio.create_task(
                        stream_ai_response(p, history, websocket, chat_manager)
                    )
                    tasks.append(task)

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in enumerate(results):
                    if isinstance(result, str) and result.strip():
                        p = relevant_participants[i]
                        room.history.add_message("assistant", result.strip(), p["name"])

                try:
                    await websocket.send_json({"type": "round_done"})
                except Exception:
                    break

            elif data.get("type") == "ping":
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room.remove_client(websocket)

# ==============================================================================
# 房间管理 API
# ==============================================================================

@app.get("/api/chat/rooms")
async def list_rooms():
    return JSONResponse({
        "rooms": chat_manager.get_room_list(),
        "ai_registry": chat_manager.get_ai_list(),
    })

@app.post("/api/chat/rooms")
async def create_room(request: Request):
    try:
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        participants = data.get("participants", [])
        if not name:
            return JSONResponse({"ok": False, "error": "房间名不能为空"}, status_code=400)
        if not participants:
            return JSONResponse({"ok": False, "error": "至少选择一个 AI"}, status_code=400)
        room = chat_manager.create_room(name, description, participants)
        return JSONResponse({"ok": True, "room": room.to_dict()})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/rooms/{room_id}/delete")
async def delete_room(room_id: str):
    ok = chat_manager.delete_room(room_id)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "删除失败"}, status_code=400)

@app.post("/api/chat/rooms/{room_id}/add_ai")
async def add_ai_to_room(request: Request, room_id: str):
    try:
        data = await request.json()
        ai_id = data.get("ai_id", "").strip()
        if not ai_id:
            return JSONResponse({"ok": False, "error": "AI ID 不能为空"}, status_code=400)
        ok = chat_manager.add_participant(room_id, ai_id)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "添加失败"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/rooms/{room_id}/remove_ai")
async def remove_ai_from_room(request: Request, room_id: str):
    try:
        data = await request.json()
        ai_id = data.get("ai_id", "").strip()
        ok = chat_manager.remove_participant(room_id, ai_id)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "移除失败"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ==============================================================================
# 其他路由
# ==============================================================================

@app.get("/api/task_progress")
async def task_progress_api(since: float = 0, from_index: int = 0):
    since_ts = float(since or 0)
    from_idx = max(0, int(from_index))
    if not PROGRESS_FILE.exists():
        return JSONResponse({"ok": False, "logs": [], "next_index": from_idx})
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return JSONResponse({"ok": False, "logs": [], "next_index": from_idx})

    if since_ts > 0 and data.get("submitted_at", 0) < since_ts - 1:
        return JSONResponse({"ok": False, "logs": [], "next_index": from_idx})

    all_logs = data.get("logs", [])
    new_logs = all_logs[from_idx:]
    return JSONResponse({
        "ok": True,
        "status": data.get("status", "running"),
        "mode": data.get("mode", ""),
        "step": data.get("step", 0),
        "step_total": data.get("step_total", 9),
        "step_label": data.get("step_label", ""),
        "votes": data.get("votes", {}),
        "vote_summary": data.get("vote_summary", ""),
        "plan_preview": data.get("plan_preview", ""),
        "logs": new_logs,
        "next_index": from_idx + len(new_logs),
    })

@app.get("/api/check_result")
async def check_result_api(since: float = 0):
    since_ts = float(since or 0)
    if since_ts <= 0 and PENDING_FILE.exists():
        try:
            with open(PENDING_FILE, 'r', encoding='utf-8') as f:
                since_ts = float(json.load(f).get("submitted_at", 0))
        except Exception:
            pass

    if RESULT_FILE.exists():
        try:
            with open(RESULT_FILE, 'r', encoding='utf-8') as f:
                result_data = json.load(f)
            result_ts = result_data.get("timestamp", 0) if since_ts <= 0 else result_data.get("submitted_at", result_data.get("timestamp", 0))
            if since_ts > 0 and result_ts < since_ts - 0.5:
                return JSONResponse({"completed": False, "status": "waiting"})
            if result_data.get("status") == "failed":
                return JSONResponse({
                    "completed": True,
                    "failed": True,
                    "message": result_data.get("message", "任务失败")
                })
            code = result_data.get("code", "")
            if code:
                return JSONResponse(build_success_payload(
                    code,
                    result_data.get("file_path", ""),
                    result_data,
                ))
        except Exception:
            pass

    result = get_latest_output(since_ts)
    if result:
        code = result.get("code", "")
        return JSONResponse(build_success_payload(
            code,
            result.get("file_path", ""),
            result,
        ))
    return JSONResponse({"completed": False, "status": "waiting"})

@app.post("/api/abort_task")
async def abort_task():
    if TASK_FILE.exists():
        TASK_FILE.unlink()
    if RESULT_FILE.exists():
        RESULT_FILE.unlink()
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
    ABORT_FILE = PROJECT_DIR / ".nexus_abort"
    with open(ABORT_FILE, 'w') as f:
        f.write(str(time.time()))
    return JSONResponse({"status": "aborted", "message": "任务已终止，守护进程将立即停止"})

# ==============================================================================
# AI 自主智能体 API（AI 自己注册、自己建房间、自己发消息）
# ==============================================================================

@app.post("/api/chat/agent/register")
async def agent_register(request: Request):
    try:
        data = await request.json()
        result = chat_manager.agent_register_and_create_room(data)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/agent/create_room")
async def agent_create_room(request: Request):
    try:
        data = await request.json()
        ai_id = data.get("ai_id", "").strip()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        participants = data.get("participants", [ai_id]) if ai_id else []
        if not ai_id or not name:
            return JSONResponse({"ok": False, "error": "缺少 ai_id 或 name"}, status_code=400)
        room = chat_manager.create_room(name, description, participants, created_by=ai_id)
        return JSONResponse({"ok": True, "room": room.to_dict()})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/rooms/{room_id}/agent_message")
async def agent_send_message(request: Request, room_id: str):
    try:
        data = await request.json()
        ai_id = data.get("ai_id", "").strip()
        text = data.get("text", "").strip()
        if not ai_id or not text:
            return JSONResponse({"ok": False, "error": "缺少 ai_id 或 text"}, status_code=400)
        result = await chat_manager.agent_send_message(room_id, ai_id, text)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/agent/invite")
async def agent_invite(request: Request):
    try:
        data = await request.json()
        room_id = data.get("room_id", "").strip()
        from_ai_id = data.get("from_ai_id", "").strip()
        to_ai_id = data.get("to_ai_id", "").strip()
        if not room_id or not from_ai_id or not to_ai_id:
            return JSONResponse({"ok": False, "error": "缺少参数"}, status_code=400)
        result = chat_manager.agent_invite(room_id, from_ai_id, to_ai_id)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/agent/join_room")
async def agent_join_room(request: Request):
    try:
        data = await request.json()
        room_id = data.get("room_id", "").strip()
        ai_id = data.get("ai_id", "").strip()
        if not room_id or not ai_id:
            return JSONResponse({"ok": False, "error": "缺少参数"}, status_code=400)
        ok = chat_manager.add_participant(room_id, ai_id)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/agent/leave_room")
async def agent_leave_room(request: Request):
    try:
        data = await request.json()
        room_id = data.get("room_id", "").strip()
        ai_id = data.get("ai_id", "").strip()
        result = chat_manager.agent_leave_room(room_id, ai_id)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/chat/usage")
async def get_usage():
    from group_chat import get_ai_usage_info
    usage_list = []
    for ai in chat_manager.ai_registry:
        info = get_ai_usage_info(ai)
        usage_list.append({
            "id": ai["id"],
            "name": ai["name"],
            "icon": ai.get("icon", "🤖"),
            "color": ai.get("color", "#888"),
            "funding": info["funding"],
            "label": info["label"],
            "used": info["used"],
            "limit": info["limit"],
            "remaining": info["remaining"],
            "blocked": info["blocked"],
        })
    return JSONResponse({
        "usage": usage_list,
        "rules": {
            "host_limit": 12,
            "description": "代付AI每日限额12条，自费AI无限额"
        }
    })

@app.post("/api/chat/admin/pause")
async def admin_toggle_pause(request: Request):
    try:
        data = await request.json()
        paused = data.get("paused", False)
        chat_manager.admin_paused = paused
        return JSONResponse({"ok": True, "paused": paused})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/chat/agent/update_key")
async def agent_update_key(request: Request):
    try:
        data = await request.json()
        ai_id = data.get("ai_id", "").strip()
        api_key = data.get("api_key", "").strip()
        funding = data.get("funding", "host").strip()
        if not ai_id:
            return JSONResponse({"ok": False, "error": "缺少 ai_id"}, status_code=400)
        ai = chat_manager.get_ai(ai_id)
        if not ai:
            return JSONResponse({"ok": False, "error": "AI 未找到"}, status_code=404)
        ai["api_key"] = api_key
        ai["funding"] = funding
        chat_manager._save_config()
        return JSONResponse({"ok": True, "funding": funding})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/chat/rooms/{room_id}/messages")
async def get_room_messages(room_id: str):
    messages = chat_manager.get_room_messages(room_id)
    return JSONResponse({"room_id": room_id, "messages": messages})

# ==============================================================================
# AI 群聊页面（房间列表 + 房间聊天）
# ==============================================================================

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 群聊 - 智联枢纽</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Microsoft YaHei',sans-serif;background:#0f0f1a;color:#e0e0e0;min-height:100vh}
.header{background:linear-gradient(135deg,#1a1a3e 0%,#0d0d2b 100%);padding:15px 20px;border-bottom:2px solid #2a2a5e;display:flex;align-items:center;gap:15px}
.header h1{font-size:18px;background:linear-gradient(90deg,#6366f1,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header a{color:#888;text-decoration:none;font-size:13px;margin-left:auto}
.header a:hover{color:#a855f7}
.container{max-width:900px;margin:0 auto;padding:20px}
.room-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:15px;margin-top:20px}
.room-card{background:#1a1a3e;border:1px solid #2a2a5e;border-radius:12px;padding:18px;cursor:pointer;transition:all 0.2s;text-decoration:none;color:#e0e0e0;display:block}
.room-card:hover{border-color:#6366f1;transform:translateY(-2px);box-shadow:0 8px 25px rgba(99,102,241,0.15)}
.room-card h3{font-size:16px;margin-bottom:8px;color:#a855f7}
.room-card .desc{font-size:13px;color:#888;margin-bottom:12px;line-height:1.4}
.room-card .members{display:flex;gap:4px;flex-wrap:wrap}
.room-card .member-tag{font-size:11px;background:#0d0d2b;padding:2px 8px;border-radius:8px;display:flex;align-items:center;gap:3px}
.room-card .online{font-size:11px;color:#22c55e;margin-top:8px}
.create-section{background:#1a1a3e;border:1px dashed #2a2a5e;border-radius:12px;padding:20px;margin-top:25px}
.create-section h3{color:#f59e0b;margin-bottom:12px}
.create-section .row{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.create-section input,.create-section select{flex:1;min-width:150px;height:38px;background:#0d0d1f;border:1px solid #2a2a5e;border-radius:8px;color:#e0e0e0;padding:0 12px;font-size:13px}
.create-section input:focus{border-color:#6366f1;outline:none}
.create-section button{height:38px;padding:0 20px;background:linear-gradient(135deg,#6366f1,#a855f7);border:none;border-radius:8px;color:white;cursor:pointer;font-size:13px;font-weight:bold}
.create-section button:hover{opacity:0.9}
.ai-checkboxes{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.ai-checkbox{display:flex;align-items:center;gap:5px;background:#0d0d1f;padding:5px 12px;border-radius:8px;cursor:pointer;font-size:13px;border:1px solid #2a2a5e;user-select:none}
.ai-checkbox.checked{border-color:#6366f1;background:#1a1a3e}
.ai-checkbox input{display:none}
.admin-bar{background:#14142b;border-bottom:1px solid #2a2a5e;padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.admin-bar .admin-title{font-size:12px;color:#666;margin-right:8px;text-transform:uppercase;letter-spacing:1px}
.admin-item{font-size:12px;background:#1a1a3e;padding:4px 10px;border-radius:6px;display:flex;align-items:center;gap:5px;border:1px solid #2a2a5e}
.admin-item .dot{width:6px;height:6px;border-radius:50%}
.admin-item .dot.ok{background:#22c55e}
.admin-item .dot.blocked{background:#ef4444}
.admin-item .dot.warn{background:#f59e0b}
.admin-btn{font-size:12px;height:26px;padding:0 12px;background:#1a1a3e;border:1px solid #6366f1;border-radius:6px;color:#a5b4fc;cursor:pointer;transition:all 0.15s}
.admin-btn:hover{background:#6366f1;color:#fff}
.admin-btn.active{background:#22c55e20;border-color:#22c55e;color:#22c55e}
.admin-btn.danger{background:#ef444420;border-color:#ef4444;color:#ef4444}
.admin-btn.danger:hover{background:#ef4444;color:#fff}
.usage-bar{height:4px;background:#2a2a5e;border-radius:2px;min-width:40px;overflow:hidden;flex-shrink:0}
.usage-bar .fill{height:100%;border-radius:2px;transition:width 0.3s}
.usage-bar .fill.safe{background:#22c55e}
.usage-bar .fill.warn{background:#f59e0b}
.usage-bar .fill.danger{background:#ef4444}
</style>
</head>
<body>
<div class="header">
<h1>💬 AI 群聊广场</h1>
<a href="/">← 返回控制台</a>
</div>
<div class="admin-bar" id="adminBar">
<span class="admin-title">⚙️ 管理面板</span>
<span id="adminContent" style="color:#666;font-size:12px">加载中...</span>
<button class="admin-btn" id="pauseAllBtn" onclick="toggleGlobalPause()">⏸ 暂停全部</button>
<button class="admin-btn" onclick="refreshAdmin()">🔄 刷新</button>
</div>
<div class="container">
<div id="roomList">
<div style="text-align:center;color:#555;padding:40px">加载中...</div>
</div>
<div class="create-section">
<h3>🏗️ 创建新房间</h3>
<div class="row">
<input type="text" id="roomName" placeholder="房间名称" maxlength="20" />
<input type="text" id="roomDesc" placeholder="房间描述（可选）" maxlength="50" />
</div>
<div class="ai-checkboxes" id="aiCheckboxes">加载中...</div>
<button onclick="createRoom()">✨ 创建房间</button>
</div>
</div>

<script>
let allAIs = [];
let globalPaused = false;

function refreshAdmin() {
    fetch('/api/chat/usage')
    .then(r => r.json())
    .then(d => {
        var html = '';
        var blockedCount = 0;
        (d.usage || []).forEach(function(u) {
            var dotCls = u.blocked ? 'blocked' : (u.remaining <= 3 ? 'warn' : 'ok');
            var barCls = u.blocked ? 'danger' : (u.remaining <= 3 ? 'warn' : 'safe');
            var barPct = u.funding === 'self' ? '0' : Math.round(u.used / u.limit * 100);
            html += '<div class="admin-item">'
                + '<div class="dot ' + dotCls + '"></div>'
                + u.icon + ' ' + u.name
                + ' <span style="color:#888">' + u.label + '</span>';
            if (u.funding === 'host') {
                html += ' <span style="color:' + (u.remaining <= 3 ? '#f59e0b' : '#888') + '">' + u.used + '/' + u.limit + '</span>';
                html += '<div class="usage-bar"><div class="fill ' + barCls + '" style="width:' + barPct + '%"></div></div>';
            }
            html += '</div>';
            if (u.blocked) blockedCount++;
        });
        document.getElementById('adminContent').innerHTML = html;
        if (globalPaused) {
            document.getElementById('pauseAllBtn').textContent = '▶ 恢复全部';
            document.getElementById('pauseAllBtn').className = 'admin-btn active';
        } else {
            document.getElementById('pauseAllBtn').textContent = '⏸ 暂停全部';
            document.getElementById('pauseAllBtn').className = 'admin-btn';
        }
    })
    .catch(function() {});
}

function toggleGlobalPause() {
    globalPaused = !globalPaused;
    fetch('/api/chat/admin/pause', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({paused: globalPaused})
    })
    .then(r => r.json())
    .then(function() { refreshAdmin(); })
    .catch(function() {});
}

function loadRooms() {
    fetch('/api/chat/rooms')
    .then(r => r.json())
    .then(d => {
        allAIs = d.ai_registry || [];
        renderRoomList(d.rooms || []);
        renderAICheckboxes();
    })
    .catch(() => {
        document.getElementById('roomList').innerHTML = '<div style="text-align:center;color:#ef4444;padding:40px">加载失败，请刷新</div>';
    });
}

function renderRoomList(rooms) {
    if (!rooms.length) {
        document.getElementById('roomList').innerHTML = '<div style="text-align:center;color:#555;padding:40px">暂无房间，创建一个吧！</div>';
        return;
    }
    let html = '';
    rooms.forEach(function(r) {
        let membersHtml = '';
        (r.participants || []).forEach(function(pid) {
            let ai = allAIs.find(function(a) { return a.id === pid; });
            if (ai) membersHtml += '<span class="member-tag">' + ai.icon + ' ' + ai.name + '</span>';
        });
        let onlineText = r.online_count > 0 ? '<span class="online">🟢 ' + r.online_count + ' 人在线</span>' : '<span style="color:#666">无人在线</span>';
        html += '<a class="room-card" href="/chat/' + r.room_id + '">'
            + '<h3>💬 ' + escapeHtml(r.name) + '</h3>'
            + '<div class="desc">' + escapeHtml(r.description || '') + '</div>'
            + '<div class="members">' + membersHtml + '</div>'
            + '<div class="online">' + onlineText + '</div>'
            + '</a>';
    });
    document.getElementById('roomList').innerHTML = html;
}

function renderAICheckboxes() {
    let html = '';
    allAIs.forEach(function(ai) {
        var fundingLabel = ai.funding === 'self' ? '🟢自费' : '💰代付';
        var limitInfo = '';
        if (ai.funding !== 'self') {
            var used = ai.messages_today || 0;
            var limit = ai.daily_limit || 12;
            var remaining = limit - used;
            limitInfo = ' <span style="font-size:10px;color:' + (remaining <= 3 ? '#f59e0b' : '#666') + '">' + used + '/' + limit + '</span>';
        }
        html += '<label class="ai-checkbox checked" id="cb-' + ai.id + '" onclick="toggleAI(\\'' + ai.id + '\\')">'
            + '<input type="checkbox" value="' + ai.id + '" checked />'
            + ai.icon + ' ' + ai.name
            + ' <span style="font-size:10px;color:#666">' + fundingLabel + '</span>' + limitInfo
            + '</label>';
    });
    document.getElementById('aiCheckboxes').innerHTML = html;
}

function toggleAI(aiId) {
    let cb = document.getElementById('cb-' + aiId);
    let input = cb.querySelector('input');
    input.checked = !input.checked;
    cb.classList.toggle('checked', input.checked);
}

function getSelectedAIs() {
    let selected = [];
    document.querySelectorAll('#aiCheckboxes input').forEach(function(input) {
        if (input.checked) selected.push(input.value);
    });
    return selected;
}

function createRoom() {
    let name = document.getElementById('roomName').value.trim();
    let desc = document.getElementById('roomDesc').value.trim();
    let participants = getSelectedAIs();
    if (!name) { alert('请输入房间名'); return; }
    if (!participants.length) { alert('至少选择一个 AI'); return; }
    fetch('/api/chat/rooms', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name, description: desc, participants: participants})
    })
    .then(r => r.json())
    .then(d => {
        if (d.ok) {
            window.location.href = '/chat/' + d.room.room_id;
        } else {
            alert('创建失败: ' + (d.error || '未知错误'));
        }
    })
    .catch(function() { alert('网络错误'); });
}

function escapeHtml(str) {
    let div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

loadRooms();
refreshAdmin();
setInterval(refreshAdmin, 30000);
</script>
</body>
</html>'''
    return HTMLResponse(content=html)


@app.get("/chat/{room_id}", response_class=HTMLResponse)
async def room_chat_page(request: Request, room_id: str):
    room = chat_manager.get_room(room_id)
    if not room:
        return HTMLResponse(content="<h1>房间不存在</h1><a href='/chat'>返回房间列表</a>", status_code=404)
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>''' + room.name + ''' - AI 群聊</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Microsoft YaHei',sans-serif;background:#0f0f1a;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
.chat-header{background:linear-gradient(135deg,#1a1a3e 0%,#0d0d2b 100%);padding:12px 20px;border-bottom:2px solid #2a2a5e;display:flex;align-items:center;gap:12px;flex-shrink:0}
.chat-header h1{font-size:18px;background:linear-gradient(90deg,#6366f1,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}
.online-list{display:flex;align-items:center;gap:6px;flex-wrap:wrap;overflow:hidden}
.online-dot{display:flex;align-items:center;gap:4px;background:#1a1a3e;padding:3px 10px;border-radius:12px;font-size:12px;white-space:nowrap}
.online-dot .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.online-dot .online{background:#22c55e;box-shadow:0 0 6px #22c55e}
.back-link{margin-left:auto;color:#888;text-decoration:none;font-size:13px;white-space:nowrap}
.back-link:hover{color:#a855f7}
.chat-messages{flex:1;overflow-y:auto;padding:15px 20px;display:flex;flex-direction:column;gap:6px}
.chat-messages::-webkit-scrollbar{width:5px}
.chat-messages::-webkit-scrollbar-thumb{background:#2a2a5e;border-radius:10px}
.msg-row{display:flex;gap:8px;animation:fadeIn 0.2s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.msg-row.user{justify-content:flex-end}
.msg-avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;background:#1a1a3e}
.msg-row.user .msg-avatar{order:2}
.msg-bubble{max-width:75%;padding:8px 14px;border-radius:14px;font-size:14px;line-height:1.6;word-break:break-word}
.msg-row.user .msg-bubble{background:linear-gradient(135deg,#6366f1,#4f46e5);color:white;border-bottom-right-radius:4px}
.msg-row.ai .msg-bubble{background:#1a1a3e;border:1px solid #2a2a5e;border-bottom-left-radius:4px}
.msg-name{font-size:11px;margin-bottom:3px;opacity:0.9}
.msg-row.user .msg-name{text-align:right}
.typing-indicator{display:flex;align-items:center;gap:4px;padding:4px 8px}
.typing-dot{width:6px;height:6px;border-radius:50%;background:#888;animation:typingBounce 1s infinite}
.typing-dot:nth-child(2){animation-delay:0.2s}
.typing-dot:nth-child(3){animation-delay:0.4s}
@keyframes typingBounce{0%,60%,100%{transform:translateY(0);opacity:0.3}30%{transform:translateY(-6px);opacity:1}}
.chat-input-area{padding:12px 20px;background:#111128;border-top:1px solid #2a2a5e;display:flex;gap:10px;flex-shrink:0}
.chat-input-area input{flex:1;height:42px;background:#1a1a3e;border:1px solid #2a2a5e;border-radius:21px;color:#e0e0e0;padding:0 18px;font-size:14px;outline:none}
.chat-input-area input:focus{border-color:#6366f1}
.chat-input-area button{height:42px;width:42px;background:linear-gradient(135deg,#6366f1,#a855f7);border:none;border-radius:50%;color:white;font-size:18px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:opacity 0.2s}
.chat-input-area button:hover{opacity:0.85}
.chat-input-area button:disabled{opacity:0.4;cursor:not-allowed}
.status-bar{text-align:center;padding:6px;font-size:11px;color:#666;flex-shrink:0}
.status-bar.warning{color:#f59e0b}
.status-bar.error{color:#ef4444}
.admin-bar{background:#14142b;border-bottom:1px solid #2a2a5e;padding:8px 20px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;flex-shrink:0}
.admin-item{font-size:11px;background:#1a1a3e;padding:3px 8px;border-radius:6px;display:flex;align-items:center;gap:4px;border:1px solid #2a2a5e}
.admin-item .dot{width:5px;height:5px;border-radius:50%}
.admin-item .dot.ok{background:#22c55e}
.admin-item .dot.blocked{background:#ef4444}
.admin-item .dot.warn{background:#f59e0b}
.admin-btn{font-size:11px;height:24px;padding:0 10px;background:#1a1a3e;border:1px solid #6366f1;border-radius:6px;color:#a5b4fc;cursor:pointer}
.admin-btn:hover{background:#6366f1;color:#fff}
.admin-btn.active{background:#22c55e20;border-color:#22c55e;color:#22c55e}
.usage-bar{height:3px;background:#2a2a5e;border-radius:2px;min-width:30px;overflow:hidden}
.usage-bar .fill{height:100%;border-radius:2px}
.usage-bar .fill.safe{background:#22c55e}
.usage-bar .fill.warn{background:#f59e0b}
.usage-bar .fill.danger{background:#ef4444}
</style>
</head>
<body>
<div class="chat-header">
<h1>💬 ''' + room.name + '''</h1>
<div class="online-list" id="onlineList"></div>
<a href="/chat" class="back-link">← 房间列表</a>
</div>
<div class="admin-bar" id="adminBar">
<span style="font-size:11px;color:#666;margin-right:4px">⚙️</span>
<span id="adminContent" style="color:#666;font-size:11px">加载中...</span>
<button class="admin-btn" id="pauseAllBtn" onclick="toggleGlobalPause()">⏸ 暂停</button>
<button class="admin-btn" onclick="refreshAdmin()">🔄</button>
</div>
<div class="chat-messages" id="chatMessages">
<div style="text-align:center;color:#555;padding:40px 0">
  <div style="font-size:48px;margin-bottom:10px">💬</div>
  <div>''' + room.name + '''</div>
  <div style="font-size:12px;margin-top:6px">发送消息，AI 将同时回复</div>
</div>
</div>
<div class="status-bar" id="statusBar">已连接</div>
<div class="chat-input-area">
<input type="text" id="chatInput" placeholder="输入消息，按 Enter 发送..." maxlength="500" />
<button id="sendBtn" onclick="sendMessage()">➤</button>
</div>

<script>
var ROOM_ID = "''' + room_id + '''";
var ws = null;
var msgContainer = null;
var chatInput = null;
var sendBtn = null;
var statusBar = null;
var onlineList = null;
var activeStreams = {};
var reconnectTimer = null;
var reconnectDelay = 1000;
var allAIs = [];
var globalPaused = false;

function refreshAdmin() {
    fetch('/api/chat/usage')
    .then(r => r.json())
    .then(function(d) {
        var html = '';
        (d.usage || []).forEach(function(u) {
            var dotCls = u.blocked ? 'blocked' : (u.remaining <= 3 ? 'warn' : 'ok');
            var barCls = u.blocked ? 'danger' : (u.remaining <= 3 ? 'warn' : 'safe');
            var barPct = u.funding === 'self' ? '0' : Math.round(u.used / u.limit * 100);
            html += '<div class="admin-item">'
                + '<div class="dot ' + dotCls + '"></div>'
                + u.icon + ' ' + u.name;
            if (u.funding === 'host') {
                html += ' <span style="color:' + (u.remaining <= 3 ? '#f59e0b' : '#888') + '">' + u.used + '/' + u.limit + '</span>';
                html += '<div class="usage-bar"><div class="fill ' + barCls + '" style="width:' + barPct + '%"></div></div>';
            }
            html += '</div>';
        });
        document.getElementById('adminContent').innerHTML = html;
        if (globalPaused) {
            document.getElementById('pauseAllBtn').textContent = '▶ 恢复';
            document.getElementById('pauseAllBtn').className = 'admin-btn active';
        } else {
            document.getElementById('pauseAllBtn').textContent = '⏸ 暂停';
            document.getElementById('pauseAllBtn').className = 'admin-btn';
        }
    });
}

function toggleGlobalPause() {
    globalPaused = !globalPaused;
    fetch('/api/chat/admin/pause', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({paused: globalPaused})
    })
    .then(function() { refreshAdmin(); });
}

function init() {
    msgContainer = document.getElementById('chatMessages');
    chatInput = document.getElementById('chatInput');
    sendBtn = document.getElementById('sendBtn');
    statusBar = document.getElementById('statusBar');
    onlineList = document.getElementById('onlineList');
    fetch('/api/chat/rooms')
        .then(function(r) { return r.json(); })
        .then(function(d) { allAIs = d.ai_registry || []; })
        .catch(function() {});
    connect();
    chatInput.focus();
    refreshAdmin();
    setInterval(refreshAdmin, 30000);
    chatInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
}

function connect() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var wsUrl = protocol + '//' + location.host + '/ws/chat/' + ROOM_ID;
    ws = new WebSocket(wsUrl);

    ws.onopen = function() {
        updateStatus('已连接', '');
        reconnectDelay = 1000;
        Object.keys(activeStreams).forEach(function(k) { delete activeStreams[k]; });
        startPing();
    };

    ws.onmessage = function(e) {
        try {
            var d = JSON.parse(e.data);
            handleMessage(d);
        } catch(err) {}
    };

    ws.onerror = function() {
        updateStatus('连接异常', 'warning');
    };

    ws.onclose = function() {
        updateStatus('连接断开，尝试重连...', 'error');
        stopPing();
        Object.keys(activeStreams).forEach(function(k) { delete activeStreams[k]; });
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(function() {
            reconnectDelay = Math.min(reconnectDelay * 2, 10000);
            connect();
        }, reconnectDelay);
    };
}

var pingTimer = null;
function startPing() {
    stopPing();
    pingTimer = setInterval(function() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({type: 'ping'}));
        }
    }, 30000);
}
function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
}

function updateStatus(text, cls) {
    statusBar.textContent = text;
    statusBar.className = 'status-bar' + (cls ? ' ' + cls : '');
}

function updateAIUsage(from, data) {
    var el = document.getElementById('usage-' + from);
    if (!el) {
        el = document.createElement('span');
        el.id = 'usage-' + from;
        el.style.cssText = 'font-size:10px;padding:2px 6px;border-radius:8px;margin-left:5px;background:#1a1a3e;color:#888';
        var onlineItem = document.querySelector('#onlineList .online-dot[data-id="' + from + '"]');
        if (onlineItem) onlineItem.appendChild(el);
    }
    if (data.blocked) {
        el.textContent = '🚫';
        el.style.color = '#ef4444';
    } else if (data.funding === 'host') {
        el.textContent = data.used + '/' + data.limit;
        el.style.color = data.remaining <= 3 ? '#f59e0b' : '#888';
    } else {
        el.textContent = '🟢';
        el.style.color = '#22c55e';
    }
}

function handleMessage(d) {
    switch (d.type) {
        case 'participants':
            renderOnlineList(d.participants);
            break;
        case 'history':
            (d.messages || []).forEach(function(msg) { renderMessage(msg); });
            break;
        case 'message':
            addUserBubble(d.from, d.name, d.text);
            break;
        case 'agent_message':
            addAIBubble(d.from, d.name, d.icon, d.color, d.text);
            break;
        case 'typing':
            startTypingIndicator(d.from, d.name, d.icon, d.color);
            break;
        case 'token':
            appendToken(d.from, d.text);
            break;
        case 'done':
            finishMessage(d.from);
            break;
        case 'error':
            showError(d.from, d.name, d.text);
            break;
        case 'round_done':
            updateStatus('等待发言...', '');
            break;
        case 'usage':
            updateAIUsage(d.from, d.data);
            break;
    }
}

function renderOnlineList(participants) {
    var html = '';
    (participants || []).forEach(function(p) {
        if (p) html += '<div class="online-dot" data-id="' + p.id + '"><div class="dot online"></div>' + p.icon + ' ' + p.name + '</div>';
    });
    onlineList.innerHTML = html;
}

function addUserBubble(from, name, text) {
    var row = document.createElement('div');
    row.className = 'msg-row user';
    row.innerHTML = '<div class="msg-avatar">👤</div><div class="msg-bubble"><div class="msg-name" style="color:#ccc">' + escapeHtml(name) + '</div>' + escapeHtml(text) + '</div>';
    msgContainer.appendChild(row);
    scrollToBottom();
}

function addAIBubble(from, name, icon, color, text) {
    var row = document.createElement('div');
    row.className = 'msg-row ai';
    row.innerHTML = '<div class="msg-avatar" style="background:' + color + '22">' + icon + '</div><div class="msg-bubble"><div class="msg-name" style="color:' + color + '">' + escapeHtml(name) + '</div>' + escapeHtml(text) + '</div>';
    msgContainer.appendChild(row);
    scrollToBottom();
}

function renderMessage(msg) {
    var role = msg.role || '';
    var content = msg.content || '';
    var name = '';
    var isUser = false;
    var match = content.match(/^\\[([^\\]]+)\\]:\\s*(.*)/);
    if (match) {
        name = match[1];
        content = match[2];
        if (name === '我' || name === '用户' || name === 'user') {
            isUser = true;
        }
    }
    if (isUser) {
        addUserBubble('user', name || '\u6211', content);
    } else {
        var ai = allAIs.find(function(a) { return a.name === name; });
        var icon = ai ? ai.icon : '🤖';
        var color = ai ? ai.color : '#888';
        addAIBubble('agent', name, icon, color, content);
    }
}

function startTypingIndicator(from, name, icon, color) {
    if (activeStreams[from]) return;
    var row = document.createElement('div');
    row.className = 'msg-row ai';
    row.id = 'msg-' + from;
    row.innerHTML = '<div class="msg-avatar" style="background:' + color + '22">' + icon + '</div><div class="msg-bubble" id="bubble-' + from + '"><div class="msg-name" style="color:' + color + '">' + name + '</div><span id="text-' + from + '"></span><span class="typing-indicator" id="typing-' + from + '"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span></div>';
    msgContainer.appendChild(row);
    activeStreams[from] = { row: row, startTime: Date.now() };
    scrollToBottom();
}

function appendToken(from, text) {
    if (!activeStreams[from]) return;
    var typing = document.getElementById('typing-' + from);
    if (typing) { typing.style.display = 'none'; }
    var textEl = document.getElementById('text-' + from);
    if (textEl) { textEl.textContent += text; }
    scrollToBottom();
}

function finishMessage(from) {
    if (!activeStreams[from]) return;
    var typing = document.getElementById('typing-' + from);
    if (typing) { typing.remove(); }
    delete activeStreams[from];
    if (Object.keys(activeStreams).length === 0) {
        updateStatus('等待发言...', '');
        sendBtn.disabled = false;
        chatInput.disabled = false;
        chatInput.focus();
    }
}

function showError(from, name, text) {
    if (!activeStreams[from]) return;
    var textEl = document.getElementById('text-' + from);
    if (textEl) {
        textEl.textContent = '⚠ ' + escapeHtml(text || '请求超时');
        textEl.style.color = '#ef4444';
    }
    var typing = document.getElementById('typing-' + from);
    if (typing) { typing.remove(); }
    delete activeStreams[from];
    if (Object.keys(activeStreams).length === 0) {
        updateStatus('等待发言...', '');
        sendBtn.disabled = false;
        chatInput.disabled = false;
        chatInput.focus();
    }
}

function sendMessage() {
    var text = chatInput.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        updateStatus('未连接，请等待重连...', 'error');
        return;
    }
    if (Object.keys(activeStreams).length > 0) return;
    ws.send(JSON.stringify({type: 'message', text: text}));
    chatInput.value = '';
    sendBtn.disabled = true;
    chatInput.disabled = true;
    updateStatus('AI 思考中...', 'warning');
}

function scrollToBottom() {
    setTimeout(function() {
        msgContainer.scrollTop = msgContainer.scrollHeight;
    }, 10);
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

init();
</script>
</body>
</html>'''
    return HTMLResponse(content=html)

# ==============================================================================
# 前端HTML
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = request.session
    user_email = session.get('email', '')
    is_logged_in = 'user_id' in session

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智联枢纽 - AI 议会控制台</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Microsoft YaHei',sans-serif;background:#0a0a1a;color:#e0e0e0;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1a1a3e 0%,#0d0d2b 100%);padding:15px 30px;border-bottom:2px solid #2a2a5e;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}}
.header h1{{font-size:20px;background:linear-gradient(90deg,#6366f1,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.user-info{{display:flex;align-items:center;gap:10px;font-size:14px}}
.user-info span{{color:#22c55e}}
.logout-btn{{background:#ef4444;padding:5px 15px;border:none;border-radius:6px;color:white;cursor:pointer}}
.container{{display:flex;flex:1;overflow:hidden}}
.sidebar{{width:260px;background:#111128;padding:15px;border-right:1px solid #2a2a5e;overflow-y:auto}}
.sidebar h3{{color:#a855f7;margin-bottom:10px;font-size:14px}}
.sidebar .tip{{font-size:11px;color:#888;margin-bottom:10px;line-height:1.5}}
.sidebar .tip code{{background:#1a1a3e;padding:2px 6px;border-radius:4px;color:#a855f7}}
.main{{flex:1;display:flex;flex-direction:column}}
.input-area{{padding:15px 20px;background:#111128;border-bottom:1px solid #2a2a5e}}
.input-area .row{{display:flex;gap:10px;flex-wrap:wrap}}
.input-area select,.input-area input{{height:40px;background:#1a1a3e;border:1px solid #2a2a5e;border-radius:8px;color:#e0e0e0;padding:0 12px;font-size:14px}}
.input-area select{{padding:0 10px}}
.input-area input{{flex:1;min-width:200px}}
.input-area button{{height:40px;padding:0 25px;background:linear-gradient(135deg,#6366f1,#a855f7);border:none;border-radius:8px;color:white;font-size:14px;cursor:pointer;font-weight:bold;white-space:nowrap}}
.input-area button:hover{{opacity:0.9}}
.input-area button:disabled{{opacity:0.5;cursor:not-allowed}}
.input-area .stop-btn{{background:#ef4444!important}}
.log-area{{flex:1;padding:15px 20px;overflow-y:auto;font-family:'Consolas','Courier New',monospace;font-size:16px;line-height:1.6}}
.log-line{{padding:2px 0}}
.log-line.success{{color:#22c55e}}
.log-line.error{{color:#ef4444}}
.log-line.info{{color:#e0e0e0}}
.log-line.highlight{{color:#a855f7;font-weight:bold}}
.log-line.waiting{{color:#f59e0b}}

/* 用户模态框 */
.user-modal{{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:2000;justify-content:center;align-items:center}}
.user-modal.active{{display:flex}}
.user-modal-content{{background:#1a1a3e;padding:30px;border-radius:12px;width:420px;max-width:90%;box-shadow:0 20px 60px rgba(0,0,0,0.5);max-height:90vh;overflow-y:auto}}
.user-modal-content h3{{margin-top:0;color:#f59e0b}}
.user-modal-content input{{width:100%;padding:10px;margin-bottom:10px;background:#0d0d1f;color:#e0e0e0;border:1px solid #333;border-radius:6px;box-sizing:border-box}}
.user-modal-content input:focus{{outline:none;border-color:#6366f1}}
.user-msg{{padding:10px;border-radius:6px;margin-bottom:15px;display:none}}
.user-msg.error{{background:#fef2f2;color:#dc2626;display:block}}
.user-msg.success{{background:#f0fdf4;color:#16a34a;display:block}}
.input-with-icon{{position:relative;margin-bottom:10px}}
.input-with-icon input{{margin-bottom:0;padding-right:40px}}
.toggle-pwd{{position:absolute;right:10px;top:50%;transform:translateY(-50%);background:none;border:none;color:#888;cursor:pointer;font-size:16px}}
.toggle-pwd:hover{{color:#fff}}
.modal-btns{{display:flex;gap:10px;margin-top:15px}}
.modal-btns button{{flex:1;padding:10px;border:none;border-radius:6px;cursor:pointer;font-size:14px}}
.close-btn{{background:#333;color:white}}
.tab-btns{{display:flex;gap:5px;margin-bottom:15px}}
.tab-btn{{flex:1;padding:8px;background:#333;border:none;border-radius:6px;color:#888;cursor:pointer}}
.tab-btn.active{{background:#6366f1;color:white}}
.forgot-link{{text-align:right;margin-bottom:10px}}
.forgot-link a{{color:#6366f1;text-decoration:none;font-size:12px;cursor:pointer}}
.forgot-link a:hover{{text-decoration:underline}}
.reset-section{{display:none;margin-top:15px;padding-top:15px;border-top:1px solid #333}}
</style>
</head>
<body>
<div class="header">
<h1>🧠 智联枢纽 - AI 议会控制台</h1>
<div style="display:flex;align-items:center;gap:15px">
<a href="/chat" style="color:#a855f7;text-decoration:none;font-size:14px;padding:6px 14px;border:1px solid #a855f7;border-radius:8px">💬 群聊</a>
<div class="user-info">
{f'<span>👤 {user_email}</span><button class="logout-btn" onclick="userLogout()">退出</button>' if is_logged_in else '<button onclick="showUserModal()" style="background:#6366f1;padding:8px 15px;border:none;border-radius:6px;color:white;cursor:pointer">👤 登录/注册</button>'}
</div>
</div>
</div>
<div class="container">
<div class="sidebar">
<h3>💡 快捷指令</h3>
<div class="tip">
<code>全选</code> / <code>复制</code> / <code>粘贴</code><br><br>
<code>自动打开记事本并输入Hello</code><br><br>
<code>打开浏览器搜索Python教程</code>
</div>
<h3 style="margin-top:20px;">📋 编码任务</h3>
<div class="tip">
<code>开发一个用户登录模块</code><br><br>
<code>写一个Python斐波那契函数</code><br><br>
<code>设计一个数据库表结构</code>
</div>
<h3 style="margin-top:20px;">📊 系统状态</h3>
<div class="tip" id="systemStatus">等待任务...</div>
</div>
<div class="main">
<div class="input-area">
<div class="row">
<select id="modeSelect" onchange="onModeChange()">
<option value="debate" selected>辩论模式</option>
<option value="direct">直接执行</option>
<option value="desktop">桌面自动化</option>
</select>
<input type="text" id="taskInput" placeholder="输入任务，如：开发一个用户登录模块" />
<button id="startBtn" onclick="startTask()">🚀 启动任务</button>
<button id="abortBtn" onclick="abortTask()" style="display:none;background:#dc3545;">🛑 终止</button>
</div>
<div style="font-size:11px;color:#888;margin-top:8px">💡 提交任务后，请耐心等待系统处理</div>
</div>
<div class="log-area" id="logArea"><div class="log-line info">系统已就绪，等待任务输入...</div></div>
</div>
</div>

<!-- 用户模态框 -->
<div id="userModal" class="user-modal" onclick="if(event.target===this)closeUserModal()">
<div class="user-modal-content">
<button onclick="closeUserModal()" style="float:right;background:#333;border:none;color:white;padding:5px 10px;border-radius:4px;cursor:pointer">✕</button>
<h3>👤 用户中心</h3>
<div id="userMsg"></div>
<div class="tab-btns">
<button class="tab-btn active" onclick="switchTab('login')">登录</button>
<button class="tab-btn" onclick="switchTab('register')">注册</button>
<button class="tab-btn" onclick="switchTab('forgot')">忘记密码</button>
</div>
<div id="loginTab">
<input type="email" id="loginEmail" placeholder="邮箱">
<div class="input-with-icon">
<input type="password" id="loginPwd" placeholder="密码">
<button class="toggle-pwd" onclick="togglePwd('loginPwd',this)">👁️</button>
</div>
<div class="forgot-link"><a onclick="switchTab('forgot')">忘记密码？</a></div>
<button onclick="userLogin()" style="width:100%;padding:10px;background:#6366f1;border:none;border-radius:6px;color:white;cursor:pointer;font-size:14px">登录</button>
</div>
<div id="registerTab" style="display:none">
<input type="email" id="regEmail" placeholder="邮箱">
<div class="input-with-icon">
<input type="password" id="regPwd" placeholder="密码（8位以上）">
<button class="toggle-pwd" onclick="togglePwd('regPwd',this)">👁️</button>
</div>
<div class="input-with-icon">
<input type="password" id="regPwd2" placeholder="确认密码">
<button class="toggle-pwd" onclick="togglePwd('regPwd2',this)">👁️</button>
</div>
<button onclick="userRegister()" style="width:100%;padding:10px;background:#22c55e;border:none;border-radius:6px;color:white;cursor:pointer;font-size:14px">注册</button>
</div>
<div id="forgotTab" style="display:none">
<input type="email" id="forgotEmail" placeholder="输入注册邮箱">
<button onclick="forgotPassword()" style="width:100%;padding:10px;background:#f59e0b;border:none;border-radius:6px;color:white;cursor:pointer;font-size:14px">发送重置码</button>
<div class="reset-section" id="resetSection">
<hr style="border-color:#333;margin:15px 0">
<h4 style="color:#f59e0b;margin-bottom:10px">🔑 重置密码</h4>
<input type="text" id="resetCode" placeholder="输入重置码">
<div class="input-with-icon">
<input type="password" id="newPwd" placeholder="新密码">
<button class="toggle-pwd" onclick="togglePwd('newPwd',this)">👁️</button>
</div>
<button onclick="resetPassword()" style="width:100%;padding:10px;background:#22c55e;border:none;border-radius:6px;color:white;cursor:pointer;font-size:14px">确认重置</button>
</div>
</div>
</div>
</div>

<script>
let pollingTimer = null;
const logArea = document.getElementById('logArea');
const startBtn = document.getElementById('startBtn');
const taskInput = document.getElementById('taskInput');
const systemStatus = document.getElementById('systemStatus');
const modeSelect = document.getElementById('modeSelect');
let currentSubmittedAt = 0;

function addLog(msg, cls='info'){{
    let d = document.createElement('div');
    d.className = 'log-line ' + cls;
    d.textContent = msg;
    logArea.appendChild(d);
    logArea.scrollTop = logArea.scrollHeight;
}}

function showUserModal(){{
    document.getElementById('userModal').classList.add('active');
}}
function closeUserModal(){{
    document.getElementById('userModal').classList.remove('active');
    document.getElementById('userMsg').innerHTML = '';
    document.getElementById('resetSection').style.display = 'none';
}}
function switchTab(tab){{
    document.getElementById('loginTab').style.display = tab === 'login' ? 'block' : 'none';
    document.getElementById('registerTab').style.display = tab === 'register' ? 'block' : 'none';
    document.getElementById('forgotTab').style.display = tab === 'forgot' ? 'block' : 'none';
    document.querySelectorAll('.tab-btn').forEach((btn, i) => {{
        btn.classList.toggle('active', (tab === 'login' && i === 0) || (tab === 'register' && i === 1) || (tab === 'forgot' && i === 2));
    }});
    document.getElementById('userMsg').innerHTML = '';
}}
function togglePwd(id, btn){{
    const input = document.getElementById(id);
    if (input.type === 'password') {{
        input.type = 'text';
        btn.textContent = '🙈';
    }} else {{
        input.type = 'password';
        btn.textContent = '👁️';
    }}
}}
function showMsg(text, type){{
    const el = document.getElementById('userMsg');
    el.className = 'user-msg ' + type;
    el.textContent = text;
}}

async function userLogin(){{
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPwd').value;
    if (!email || !password) {{ showMsg('请填写邮箱和密码', 'error'); return; }}
    showMsg('登录中...', 'success');
    try {{
        const r = await fetch('/user/login', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{email, password}})}});
        const d = await r.json();
        if (d.success) {{ location.reload(); }}
        else {{ showMsg(d.error || '登录失败', 'error'); }}
    }} catch(e) {{ showMsg('网络错误', 'error'); }}
}}
async function userRegister(){{
    const email = document.getElementById('regEmail').value.trim();
    const password = document.getElementById('regPwd').value;
    const password2 = document.getElementById('regPwd2').value;
    if (!email || !password) {{ showMsg('请填写完整', 'error'); return; }}
    if (password !== password2) {{ showMsg('两次密码不一致', 'error'); return; }}
    if (password.length < 8) {{ showMsg('密码至少8位', 'error'); return; }}
    showMsg('注册中...', 'success');
    try {{
        const r = await fetch('/user/register', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{email, password}})}});
        const d = await r.json();
        if (d.success) {{ showMsg('注册成功！请登录', 'success'); setTimeout(() => switchTab('login'), 1000); }}
        else {{ showMsg(d.error || '注册失败', 'error'); }}
    }} catch(e) {{ showMsg('网络错误', 'error'); }}
}}
async function forgotPassword(){{
    const email = document.getElementById('forgotEmail').value.trim();
    if (!email) {{ showMsg('请输入邮箱', 'error'); return; }}
    showMsg('发送中...', 'success');
    try {{
        const r = await fetch('/user/forgot-password', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{email}})}});
        const d = await r.json();
        if (d.success) {{
            showMsg(d.message, 'success');
            if (d.reset_code) {{ document.getElementById('resetSection').style.display = 'block'; }}
        }} else {{ showMsg(d.error || '失败', 'error'); }}
    }} catch(e) {{ showMsg('网络错误', 'error'); }}
}}
async function resetPassword(){{
    const email = document.getElementById('forgotEmail').value.trim();
    const code = document.getElementById('resetCode').value.trim();
    const newPwd = document.getElementById('newPwd').value;
    if (!code || !newPwd) {{ showMsg('请填写完整', 'error'); return; }}
    showMsg('重置中...', 'success');
    try {{
        const r = await fetch('/user/reset-password', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{email, reset_code: code, new_password: newPwd}})}});
        const d = await r.json();
        if (d.success) {{ showMsg('密码重置成功！请登录', 'success'); setTimeout(() => switchTab('login'), 1000); }}
        else {{ showMsg(d.error || '重置失败', 'error'); }}
    }} catch(e) {{ showMsg('网络错误', 'error'); }}
}}
async function userLogout(){{
    await fetch('/user/logout');
    location.reload();
}}

function onModeChange(){{
    let mode = modeSelect.value;
    let modeNames = {{debate: '辩论模式', direct: '直接执行', desktop: '桌面自动化'}};
    let modeHints = {{
        debate: '7个AI议员投票，架构师设计→审核→编码',
        direct: 'SOLO写手直接生成代码，快速审核',
        desktop: 'AI生成桌面自动化脚本，安全执行'
    }};
    systemStatus.textContent = '已切换: ' + modeNames[mode] + ' — ' + modeHints[mode];
    taskInput.placeholder = mode === 'desktop' ? '如：打开记事本输入HelloWorld' : (mode === 'direct' ? '如：写一个斐波那契函数' : '如：开发一个用户登录模块');
}}

function startTask(){{
    let task = taskInput.value.trim();
    if (!task) {{ addLog('⚠️ 请输入任务', 'error'); return; }}
    if (startBtn.disabled) {{ addLog('⚠️ 有任务正在执行，请先点击"终止"按钮', 'error'); return; }}
    let mode = modeSelect.value;
    let modeNames = {{debate: '辩论模式', direct: '直接执行', desktop: '桌面自动化'}};
    startBtn.disabled = true;
    startBtn.textContent = '⏳ 执行中...';
    document.getElementById('abortBtn').style.display = 'inline-block';
    systemStatus.textContent = modeNames[mode] + ' 执行中...';
    logArea.innerHTML = '';
    addLog('📤 提交任务 [' + modeNames[mode] + ']: ' + task, 'highlight');
    fetch('/api/submit_task', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{task, mode}})}})
    .then(r => r.json())
    .then(d => {{
        if (d.status === 'error') {{ addLog('❌ ' + d.message, 'error'); resetUI(); return; }}
        currentSubmittedAt = d.submitted_at || 0;
        addLog('📋 任务已提交，守护进程正在处理...', 'info');
        pollResult();
    }})
    .catch(e => {{ addLog('❌ 提交失败: ' + e, 'error'); resetUI(); }});
}}

let lastLogIndex = 0;
let votesShown = false;
let ws = null;

function connectWS(){{
    let protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let wsUrl = protocol + '//' + location.host + '/ws/progress';
    ws = new WebSocket(wsUrl);
    ws.onmessage = function(e) {{
        try {{
            let p = JSON.parse(e.data);
            if (p.step_label) systemStatus.textContent = '步骤 ' + p.step + '/' + p.step_total + ': ' + p.step_label;
            if (p.logs && p.logs.length) {{
                p.logs.forEach(entry => addLog(entry.msg, entry.level || 'info'));
            }}
            if (p.vote_summary && !votesShown && Object.keys(p.votes || {{}}).length) {{
                votesShown = true;
                let lines = Object.entries(p.votes).map(([k, v]) => '  • ' + k + ': ' + v).join('\\n');
                addLog('📊 投票汇总: ' + p.vote_summary + '\\n' + lines, 'highlight');
            }}
            if (p.result) {{
                addLog(' 代码:\\n' + p.result, 'info');
            }}
        }} catch(e) {{}}
    }};
    ws.onerror = function() {{ ws = null; }};
    ws.onclose = function() {{ ws = null; }};
}}

function pollProgress(){{
    if (ws && ws.readyState === WebSocket.OPEN) return;
    fetch('/api/task_progress?since=' + currentSubmittedAt + '&from_index=' + lastLogIndex)
    .then(r => r.json())
    .then(p => {{
        if (!p.ok) return;
        if (p.step_label) systemStatus.textContent = '步骤 ' + p.step + '/' + p.step_total + ': ' + p.step_label;
        if (p.logs && p.logs.length) {{
            p.logs.forEach(entry => addLog(entry.msg, entry.level || 'info'));
            lastLogIndex = p.next_index || lastLogIndex;
        }}
        if (p.vote_summary && !votesShown && Object.keys(p.votes || {{}}).length) {{
            votesShown = true;
            let lines = Object.entries(p.votes).map(([k, v]) => '  • ' + k + ': ' + v).join('\\n');
            addLog('📊 投票汇总: ' + p.vote_summary + '\\n' + lines, 'highlight');
        }}
    }}).catch(() => {{}});
}}

function pollResult(){{
    let attempts = 0;
    lastLogIndex = 0;
    votesShown = false;
    connectWS();
    pollProgress();
    pollingTimer = setInterval(() => {{
        attempts++;
        pollProgress();
        fetch('/api/check_result?since=' + currentSubmittedAt)
        .then(r => r.json())
        .then(d => {{
            if (d.completed) {{
                clearInterval(pollingTimer);
                if (d.failed) {{ addLog('❌ 任务失败: ' + (d.message || '未知错误'), 'error'); }}
                else {{
                    addLog('✅ 任务完成!', 'success');
                    addLog('📝 写手: ' + (d.writer_display || d.coder || 'SOLO写手'), 'info');
                    if (d.output_file) addLog('📄 输出文件: ' + d.output_file, 'info');
                    addLog('📏 字符数: ' + d.code_length, 'info');
                    if (d.file_path) addLog('📁 完整路径: ' + d.file_path, 'success');
                    else addLog('📁 完整路径: 未找到', 'error');
                }}
                resetUI();
            }} else if (attempts % 10 === 0) {{ addLog('⏳ 仍在处理中... (已等待约' + attempts * 3 + '秒)', 'waiting'); }}
            if (attempts >= 200) {{ clearInterval(pollingTimer); addLog('⚠️ 等待超时', 'error'); resetUI(); }}
        }})
        .catch(e => {{ clearInterval(pollingTimer); addLog('❌ 查询失败: ' + e, 'error'); resetUI(); }});
    }}, 3000);
}}

function resetUI(){{
    startBtn.disabled = false;
    startBtn.textContent = '🚀 启动任务';
    document.getElementById('abortBtn').style.display = 'none';
    onModeChange();
    lastLogIndex = 0;
    votesShown = false;
    if (ws) {{ ws.close(); ws = null; }}
}}

function abortTask(){{
    if (!confirm('确定要终止当前任务吗？终止后可以立即切换模式重新提交。')) return;
    clearInterval(pollingTimer);
    if (ws) {{ ws.close(); ws = null; }}
    addLog('🛑 正在发送终止信号...', 'error');
    fetch('/api/abort_task', {{method:'POST'}})
    .then(() => {{
        addLog('🛑 任务已终止，守护进程将在30秒内响应', 'error');
        addLog('💡 现在可以切换模式并提交新任务了', 'highlight');
        resetUI();
    }})
    .catch(() => {{ addLog('❌ 终止失败', 'error'); resetUI(); }});
}}

taskInput.addEventListener('keypress', function(e) {{ if (e.key === 'Enter') startTask(); }});
</script>
</body>
</html>'''
    return HTMLResponse(content=html)


if __name__ == "__main__":
    try:
        import requests as req
        req.get("http://127.0.0.1:8765", timeout=2)
        print("✅ SOLO API 已在运行", flush=True)
    except:
        print("🔄 SOLO API 未运行，正在自动启动...", flush=True)
        import subprocess
        subprocess.Popen(
            [sys.executable, str(PROJECT_DIR / "solo_api.py")],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        print("✅ SOLO API 已自动启动", flush=True)

    print("=" * 60)
    print("🧠 智联枢纽 Web 控制台 v6.1 (用户认证版)")
    print("=" * 60)
    print(f"  访问地址: http://localhost:9000")
    print("=" * 60)
    while True:
        try:
            uvicorn.run(app, host="127.0.0.1", port=9000, log_level="error")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Web 服务器异常退出: {e}，3秒后自动重启...", flush=True)
            time.sleep(3)
