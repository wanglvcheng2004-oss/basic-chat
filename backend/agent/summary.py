import json
from datetime import datetime, timezone

from .types import ROOM_THREAD


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


async def window_messages(store, start, end):
    await store.flush()
    rows = await store.db.all('SELECT payload FROM messages WHERE thread_id=? AND created_at>=? AND created_at<? ORDER BY created_at,id',
                             (ROOM_THREAD, iso(start), iso(end)))
    selected = []
    for row in rows:
        item = json.loads(row['payload'])
        task = store.get_task(item.get('task_id', ''))
        if item['role'] == 'user' or (item.get('kind') == 'task_result' and task and task.kind == 'interactive' and task.status == 'done'):
            selected.append(item)
    return selected


def bounded_transcript(messages, limit=16_000):
    lines, used, covered = [], 0, 0
    clipped = False
    for message in messages:
        content = message['content'][:2000]
        line = f"[{message['id']}] {message['created_at']} {message['nickname']}: {content}"
        if used + len(line) > limit:
            break
        lines.append(line)
        used += len(line) + 1
        covered += 1
        clipped = clipped or len(content) < len(message['content'])
    warning = ''
    if covered < len(messages) or clipped:
        warning = f'覆盖不足：本窗口共 {len(messages)} 条有效消息，本次纳入 {covered} 条，长消息最多保留 2000 字符。'
    return '\n'.join(lines), covered, warning
