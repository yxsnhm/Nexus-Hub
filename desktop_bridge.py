"""
 -  (DesktopBridge)



1. DSL
2. AI""
3. 
4. +

DSL
{
  "steps": [
    {"action": "open_app", "params": {"app": "notepad.exe"}},
    {"action": "wait", "params": {"seconds": 1}},
    {"action": "type", "params": {"text": "Hello World"}},
    {"action": "screenshot", "params": {"filename": "result.png"}}
  ]
}
"""

import json
import time
import os
import threading
from typing import Optional, Dict, Any, List, Callable
from nexus_protocol import BaseBridge, CouncilorIdentity, CouncilMessage, MessageType
from desktop_automator import DesktopAutomator, SAFETY_MODE, CONFIRM_HIGH_RISK


ACTION_MAP = {
    # 
    "move": "mouse_move",
    "click": "mouse_click",
    "double_click": "mouse_double_click",
    "drag": "mouse_drag",
    "scroll": "mouse_scroll",
    # 
    "type": "keyboard_type",
    "hotkey": "keyboard_hotkey",
    "press": "keyboard_press",
    # 
    "find_window": "window_find",
    "activate_window": "window_activate",
    "resize_window": "window_resize",
    "close_window": "window_close",
    "open_app": "open_app",
    # 
    "screenshot": "screenshot",
    "locate": "locate_image",
    "pixel": "get_pixel",
    # 
    "clipboard_get": "clipboard_get",
    "clipboard_set": "clipboard_set",
    "clipboard_clear": "clipboard_clear",
    # 
    "wait": "wait",
    "wait_image": "wait_for_image",
    "wait_window": "wait_for_window",
    # 
    "click_image": "click_image",
    "type_into": "type_into",
}

HIGH_RISK_ACTIONS = {"close_window", "clipboard_clear", "press"}


class DesktopBridge(BaseBridge):
    """
    
    AI
    """

    def __init__(self, identity: CouncilorIdentity, safety_mode: bool = SAFETY_MODE):
        super().__init__(identity)
        self.automator = DesktopAutomator(safety_mode=safety_mode)
        self.task_history: List[Dict] = []
        self.auto_heal_enabled = False

    def connect(self) -> bool:
        """"""
        try:
            screen = self.automator.get_screen_size()
            self.connected = True
            print(f"   [{self.identity.name}] ")
            print(f"     : {screen.width}x{screen.height}")
            print(f"     : {'' if SAFETY_MODE else ''}")
            print(f"     : {'' if CONFIRM_HIGH_RISK else ''}")
            return True
        except Exception as e:
            print(f"   [{self.identity.name}] : {e}")
            return False

    def disconnect(self):
        self.connected = False
        self.automator.audit.save_to_file(f"desktop_audit_{int(time.time())}.json")
        print(f"   [{self.identity.name}] ")

    def send_message(self, message: CouncilMessage) -> bool:
        """"""
        self.task_history.append({
            "sender": message.sender.name,
            "type": message.msg_type.value,
            "content": message.content[:200],
            "timestamp": time.time()
        })
        return True

    def think_and_speak(self, topic: str, msg_type: MessageType = MessageType.SPEECH) -> Optional[CouncilMessage]:
        """
        

        topic 
        1. JSON DSL - 
        2.  - AI
        """
        task_start = time.time()

        steps = self._parse_task(topic)
        if not steps:
            return CouncilMessage(
                message_id=f"desktop-{int(time.time())}",
                session_id="desktop-session",
                sender=self.identity,
                msg_type=MessageType.ERROR,
                content=" DSL"
            )

        print(f"    {len(steps)} ...")
        results = []
        all_ok = True

        for i, step in enumerate(steps):
            step_num = i + 1
            action = step.get("action", "")
            params = step.get("params", {})

            if action not in ACTION_MAP:
                results.append({"step": step_num, "action": action, "status": "failed", "error": ""})
                all_ok = False
                continue

            if action in HIGH_RISK_ACTIONS and CONFIRM_HIGH_RISK:
                confirmed = self.automator.confirm_action(
                    f"{step_num}: {action}({params})", risk_level="high"
                )
                if not confirmed:
                    results.append({"step": step_num, "action": action, "status": "cancelled"})
                    all_ok = False
                    continue

            method_name = ACTION_MAP[action]
            method = getattr(self.automator, method_name, None)
            if not method:
                results.append({"step": step_num, "action": action, "status": "failed", "error": ""})
                all_ok = False
                continue

            try:
                if isinstance(params, dict):
                    result = method(**params)
                elif isinstance(params, list):
                    result = method(*params)
                else:
                    result = method(params)

                status = "ok" if result not in (False, None, []) else "ok" if result == [] else "failed"
                results.append({"step": step_num, "action": action, "status": status})
                if status == "failed":
                    all_ok = False
            except Exception as e:
                results.append({"step": step_num, "action": action, "status": "failed", "error": str(e)})
                all_ok = False

        elapsed = round(time.time() - task_start, 1)
        report = self._build_report(steps, results, elapsed, all_ok)

        print(f"  {'' if all_ok else ''}  ({elapsed}s, {len(results)})")
        if not all_ok:
            failed = [r for r in results if r["status"] != "ok"]
            print(f"  {len(failed)} ")

        msg_type = MessageType.TASK_REPORT if all_ok else MessageType.ERROR
        return CouncilMessage(
            message_id=f"desktop-{int(time.time())}",
            session_id="desktop-session",
            sender=self.identity,
            msg_type=msg_type,
            content=report
        )

    def _parse_task(self, topic: str) -> List[Dict]:
        """JSON"""
        topic = topic.strip()

        try:
            data = json.loads(topic)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("steps", [data])
        except json.JSONDecodeError:
            pass

        if topic.startswith("{") or topic.startswith("["):
            return []

        lines = [l.strip() for l in topic.split("\n") if l.strip()]
        steps = []
        for line in lines:
            for action_name in ["click", "type", "wait", "press", "hotkey", "screenshot",
                                "open", "move", "scroll", "activate", "close"]:
                if line.lower().startswith(action_name):
                    params = self._parse_natural_params(line, action_name)
                    steps.append({"action": action_name, "params": params})
                    break
        return steps

    def _parse_natural_params(self, line: str, action: str) -> Dict:
        """"""
        rest = line[len(action):].strip()
        params = {}

        if action == "click":
            import re
            coords = re.findall(r"(\d+)", rest)
            if len(coords) >= 2:
                params = {"x": int(coords[0]), "y": int(coords[1])}
            elif "right" in rest.lower():
                params = {"button": "right"}
            elif "double" in rest.lower():
                params = {"clicks": 2}
        elif action == "type":
            text = rest.strip("\"' ")
            if text:
                params = {"text": text}
        elif action == "wait":
            import re
            nums = re.findall(r"(\d+\.?\d*)", rest)
            params = {"seconds": float(nums[0]) if nums else 1.0}
        elif action == "hotkey":
            keys = [k.strip() for k in rest.replace("+", " ").split()]
            if keys:
                params = {"keys": tuple(keys)}
        elif action == "open":
            params = {"app": rest.strip("\"' ")}

        return params

    def _build_report(self, steps: List[Dict], results: List[Dict],
                      elapsed: float, all_ok: bool) -> str:
        """"""
        audit = self.automator.audit.summary()
        lines = []
        lines.append(" ")
        lines.append("=" * 40)
        lines.append(f": {' ' if all_ok else ' '}")
        lines.append(f": {len(steps)} ,  {elapsed}s")
        lines.append(f": {audit['succeeded']} / {audit['failed']}")

        lines.append(f"\n{''*40}")
        lines.append(":")
        for r in results:
            icon = "" if r["status"] == "ok" else "" if r["status"] == "cancelled" else ""
            error = f" - {r.get('error', '')}" if "error" in r else ""
            lines.append(f"  {icon} {r['step']}: {r['action']}{error}")

        lines.append(f"\n{''*40}")
        lines.append(":")
        lines.append(f"  : {audit['total_operations']}")
        lines.append(f"  : {audit['succeeded']}")
        lines.append(f"  : {audit['failed']}")
        lines.append(f"  : {audit['duration']}s")

        return "\n".join(lines)

    def execute_dsl(self, dsl: Dict) -> Dict:
        """DSLhub"""
        steps = dsl if isinstance(dsl, list) else dsl.get("steps", [])
        msg = self.think_and_speak(json.dumps({"steps": steps}))
        return {
            "report": msg.content if msg else "",
            "audit": self.automator.audit.summary(),
            "screenshots": os.listdir(self.automator.last_screenshot_dir),
        }

    def simulate(self, dsl: Dict) -> str:
        """"""
        steps = dsl if isinstance(dsl, list) else dsl.get("steps", [])
        lines = ["  - "]
        lines.append(f"{'='*40}")
        lines.append(f" {len(steps)} \n")
        for i, step in enumerate(steps):
            action = step.get("action", "?")
            params = step.get("params", {})
            risk = " " if action in HIGH_RISK_ACTIONS else " "
            lines.append(f"  [{i+1}] {risk} {action}({params})")
        lines.append(f"\n{'='*40}")
        lines.append("")
        return "\n".join(lines)