# src/libraries/config_env.py
"""
统一的 .env 加载与读取

nonebot 启动时只把 .env 读进它自己的 Config 对象，**不会**写进 os.environ，
所以插件里直接 os.getenv(...) 是读不到 .env 的（只能读到真正的进程环境变量）。
这个模块负责把项目根目录的 .env / .env.<ENVIRONMENT> 加载进环境变量，
插件在读配置前调用一次 load_project_env() 即可。

规则：已存在的进程环境变量优先，不会被 .env 覆盖（load_dotenv override=False）。
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_loaded = False


def load_project_env() -> None:
    """把 .env 加载进 os.environ（幂等，只真正执行一次）"""
    global _loaded
    if _loaded:
        return
    _loaded = True

    root = Path(__file__).resolve().parents[2]  # src/libraries/config_env.py -> 项目根目录
    env_name = os.getenv("ENVIRONMENT") or "prod"
    candidates = [
        Path.cwd() / ".env",
        root / ".env",
        Path.cwd() / f".env.{env_name}",
        root / f".env.{env_name}",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取配置：先加载 .env，再取环境变量"""
    load_project_env()
    return os.getenv(name, default)
