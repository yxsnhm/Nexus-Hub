import pyautogui, time, os

class DesktopAutomation:
    def execute(self, task):
        if "记事本" in task and "输入" in task:
            os.system("notepad")
            time.sleep(1)
            pyautogui.write("Hello")
            return {"file": "桌面操作完成", "code": "pyautogui.write('Hello')", "lines": 1}
        return None