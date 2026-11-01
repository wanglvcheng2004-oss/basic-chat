import time


class Watchdog:
    """No model calls. A quiet provider is suspected, never blindly restarted."""
    def __init__(self, runtime):
        self.runtime = runtime

    async def tick(self, now=None):
        now = time.time() if now is None else now
        for task in list(self.runtime.store.tasks.values()):
            if task.status != 'running':
                continue
            if task.deadline_at and now >= task.deadline_at:
                await self.runtime.cancel_task(task, 'timeout')
                continue
            stalled = task.last_signal_at and now - task.last_signal_at >= self.runtime.config.stall_seconds
            health = 'suspected_stall' if stalled else 'normal'
            if health != task.health:
                self.runtime.store.update_task(task, health=health, health_revision=task.health_revision + 1)
                await self.runtime.notice(task, '疑似停滞：较长时间没有新执行信号。' if stalled else '任务已恢复执行信号。', f'health:{task.health_revision}')
                await self.runtime.emit_state(task)
