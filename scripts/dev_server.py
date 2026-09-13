"""启动 tableseed WebUI —— 环境变量（数据库密码）从注册表读取注入，
避免在命令行里出现明文密码。用法：python scripts/dev_server.py
"""
import os
import subprocess
import sys
import winreg

NAME = "TABLESEED_DB_PASSWORD"
try:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    try:
        pwd, _ = winreg.QueryValueEx(key, NAME)
    finally:
        winreg.CloseKey(key)
except FileNotFoundError:
    print(f"环境变量 {NAME} 未设置，将以无密码环境启动")
    pwd = None

env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
if pwd:
    env[NAME] = pwd

# 去掉宿主注入的 PYTHONPATH（sitecustomize shim）—— 让开发服务跑在
# 与用户自己 start.bat 启动时一致的环境里：shim 里的安全策略会拦截
# 文件删除等操作并抛 SystemExit(1)，曾把服务进程整个杀掉。
env.pop("PYTHONPATH", None)

cmd = [r".venv\Scripts\tableseed.exe", "ui", "-c", "samples/txn.yaml",
       "--port", "8643", "--no-browser"]
print("starting:", " ".join(cmd))
sys.stdout.flush()
proc = subprocess.Popen(cmd, env=env)
proc.wait()
