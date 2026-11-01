"""Server-side data scopes; prompts and browser fields are not access checks."""
from .agent.types import ROOM_THREAD, dm_thread


def can_read_thread(actor, thread_id: str) -> bool:
    if thread_id == ROOM_THREAD:
        return True
    return actor.channel == 'dm' and thread_id == dm_thread(actor.owner_id)


def can_view_task(member_id: str, task) -> bool:
    return task.channel == 'room' or task.owner_id == member_id
