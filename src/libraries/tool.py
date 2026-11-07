import re
import time

# 本机绝对路径（Windows 盘符、file:/// URI）
_WIN_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\s，。；：、!?\"'）)】\]]*")
_FILE_URI_PATTERN = re.compile(r"file:///[^\s，。；：、!?\"'）)】\]]*")


def hide_local_paths(text: str) -> str:
    """
    把要发到聊天里的文本中的本机绝对路径遮掉（控制台日志不受影响）。

    例如 "C:\\Users\\xxx\\data\\bilibili\\a.mp4" -> "<本机路径>"
    """
    if not text:
        return text
    text = _FILE_URI_PATTERN.sub("<本机路径>", str(text))
    return _WIN_PATH_PATTERN.sub("<本机路径>", text)


def hash(qq: int):
    days = int(time.strftime("%d", time.localtime(time.time()))) + 31 * int(
        time.strftime("%m", time.localtime(time.time()))) + 77
    return (days * qq) >> 8
