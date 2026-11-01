"""Deterministic issue selection and bounded, read-only source snapshots."""
import hashlib
import re

from . import tools

EXTENSIONS = r'(?:py|js|ts|tsx|jsx|vue|css|html|md|txt|cpp|hpp|h|c|rs|go|json|yaml|yml)'
QUOTED = re.compile(r'["\'`]([^"\'`\n]+\.' + EXTENSIONS + r')["\'`]')
UNQUOTED = re.compile(r'(?<![\w])(?:[A-Za-z]:[\\/])?[\w./\\-]+\.' + EXTENSIONS + r'\b')


def paths_in(text):
    return list(dict.fromkeys(QUOTED.findall(text) + UNQUOTED.findall(text)))[:6]


def is_issue(message, agent_name):
    if message.role != 'user' or message.channel != 'room' or message.kind != 'chat':
        return False
    if re.search(r'@(?:' + re.escape(agent_name) + r'|agent)(?=$|\s|[，。！？,.!?;；:：])', message.content, re.I):
        return False
    text = message.content
    traceback = 'Traceback (most recent call last)' in text or bool(re.search(r'\bat .+\(.+:\d+:\d+\)', text))
    error = bool(re.search(r'error|exception|报错|错误|失败|崩溃', text, re.I))
    return traceback or (error and bool(paths_in(text)))


def snapshot_files(text):
    files, excerpts = {}, []
    for raw in paths_in(text):
        try:
            path = tools._safe_path(raw)
            if not path.is_file() or path.stat().st_size > 256_000:
                continue
            data = path.read_bytes()
            if len(data) > 256_000 or b'\0' in data:
                continue
            name = path.relative_to(tools.ROOT).as_posix()
            if name in files:
                continue
            files[name] = hashlib.sha256(data).hexdigest()
            excerpts.append(f'文件 {name}（最多 5000 字符）：\n' + data.decode('utf-8', 'replace')[:5000])
        except (OSError, ValueError):
            continue
    return files, '\n\n'.join(excerpts)[:16_000]


def versions_match(files):
    for name, version in files.items():
        try:
            path = tools._safe_path(name)
            if not path.is_file() or path.stat().st_size > 256_000:
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != version:
                return False
        except (OSError, ValueError):
            return False
    return True


def fingerprint(text, files):
    normalized = ' '.join(text.casefold().split())
    normalized = re.sub(r'0x[0-9a-f]+', '0xADDR', normalized)
    return hashlib.sha256((normalized + repr(sorted(files.items()))).encode()).hexdigest()
