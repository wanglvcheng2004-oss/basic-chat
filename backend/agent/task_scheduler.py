from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from heapq import heappop, heappush


@dataclass(slots=True)
class ProjectPolicy:
    project_id: str
    priority: int = 3          # 1 (low) .. 5 (high)
    weight: float = 1.0        # share of worker capacity


@dataclass(slots=True)
class Job:
    id: str
    project_id: str
    urgency: int = 2           # 1 (low) .. 4 (urgent)
    created_at: float = 0.0    # seconds
    duration: float = 60.0     # seconds
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def wait(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, self.started_at - self.created_at)

    @property
    def turnaround(self) -> float:
        if self.finished_at is None:
            return 0.0
        return max(0.0, self.finished_at - self.created_at)


@dataclass(slots=True)
class SimResult:
    jobs: list[Job]
    makespan: float
    per_project_wait: dict[str, float] = field(default_factory=dict)

    @property
    def waits(self) -> list[float]:
        return sorted(job.wait for job in self.jobs)

    @property
    def turnarounds(self) -> list[float]:
        return sorted(job.turnaround for job in self.jobs)

    def summary(self) -> dict[str, float]:
        import statistics

        waits = self.waits
        turnarounds = self.turnarounds
        count = max(1, len(waits))
        p95 = waits[min(len(waits) - 1, int(0.95 * len(waits)))] if waits else 0.0
        return {
            "count": len(waits),
            "avg_wait": statistics.fmean(waits) if waits else 0.0,
            "p95_wait": p95,
            "max_wait": max(waits) if waits else 0.0,
            "avg_turnaround": statistics.fmean(turnarounds) if turnarounds else 0.0,
            "makespan": self.makespan,
            "throughput_per_min": len(waits) / max(self.makespan, 1e-9) * 60,
        }


class PriorityScheduler:
    """Project-aware priority queue with weighted quotas and aging.

    score = project.priority * 100 + urgency * 10 + waiting_minutes * aging
    """

    def __init__(
        self,
        projects: list[ProjectPolicy],
        max_workers: int = 4,
        aging_per_minute: float = 0.5,
    ) -> None:
        if not projects:
            raise ValueError("at least one project is required")
        self.projects = {project.project_id: project for project in projects}
        self.max_workers = max(1, max_workers)
        self.aging_per_minute = max(0.0, aging_per_minute)
        total_weight = sum(max(0.0, project.weight) for project in projects) or 1.0
        self.quotas = {
            project.project_id: max(1, round(max(0.0, project.weight) / total_weight * self.max_workers))
            for project in projects
        }

    def score(self, job: Job, now: float) -> float:
        project = self.projects.get(job.project_id)
        base = (project.priority if project else 3) * 100
        waiting_minutes = max(0.0, now - job.created_at) / 60.0
        return base + job.urgency * 10 + waiting_minutes * self.aging_per_minute

    def simulate(self, jobs: list[Job]) -> SimResult:
        """Discrete-event simulation. Returns completed jobs with timing."""
        waiting: list[Job] = []
        pending = sorted(jobs, key=lambda job: (job.created_at, job.id))
        index = 0
        running: list[tuple[float, str, Job]] = []
        completed: list[Job] = []
        now = 0.0

        if pending:
            now = pending[0].created_at

        while index < len(pending) or running or waiting:
            while index < len(pending) and pending[index].created_at <= now:
                waiting.append(pending[index])
                index += 1

            while len(running) < self.max_workers and waiting:
                running_counts = Counter(job.project_id for _, _, job in running)

                def selection_key(job: Job) -> tuple[float, float, str]:
                    score = self.score(job, now)
                    project = self.projects.get(job.project_id)
                    # Weight is a soft target: under-quota projects get a bonus,
                    # but a high-priority project may still exceed it.
                    if running_counts[job.project_id] < self.quotas.get(job.project_id, 1):
                        score += 30.0
                    if project is not None:
                        score += project.weight * 20.0
                    return (-score, job.created_at, job.id)

                chosen = min(waiting, key=selection_key)
                waiting.remove(chosen)
                chosen.started_at = now
                heappush(running, (now + chosen.duration, chosen.id, chosen))

            if not running:
                if index < len(pending):
                    now = max(now, pending[index].created_at)
                continue

            finish, _, job = heappop(running)
            now = max(now, finish)
            job.finished_at = now
            completed.append(job)

        per_project: dict[str, float] = {}
        for project_id in self.projects:
            waits = [job.wait for job in completed if job.project_id == project_id]
            per_project[project_id] = sum(waits) / len(waits) if waits else 0.0
        makespan = max((job.finished_at or 0.0 for job in completed), default=0.0) - min(
            (job.created_at for job in completed), default=0.0
        )
        return SimResult(jobs=completed, makespan=max(0.0, makespan), per_project_wait=per_project)

    def simulate_fifo(self, jobs: list[Job]) -> SimResult:
        """FIFO baseline: earliest arrival first, ignoring project/urgency."""
        pending = sorted(jobs, key=lambda job: (job.created_at, job.id))
        index = 0
        waiting: list[Job] = []
        running: list[tuple[float, str, Job]] = []
        completed: list[Job] = []
        now = pending[0].created_at if pending else 0.0

        while index < len(pending) or running or waiting:
            while index < len(pending) and pending[index].created_at <= now:
                waiting.append(pending[index])
                index += 1
            while len(running) < self.max_workers and waiting:
                chosen = min(waiting, key=lambda job: (job.created_at, job.id))
                waiting.remove(chosen)
                chosen.started_at = now
                heappush(running, (now + chosen.duration, chosen.id, chosen))
            if not running:
                if index < len(pending):
                    now = max(now, pending[index].created_at)
                continue
            finish, _, job = heappop(running)
            now = max(now, finish)
            job.finished_at = now
            completed.append(job)

        makespan = max((job.finished_at or 0.0 for job in completed), default=0.0) - min(
            (job.created_at for job in completed), default=0.0
        )
        per_project = {}
        for project_id in self.projects:
            waits = [job.wait for job in completed if job.project_id == project_id]
            per_project[project_id] = sum(waits) / len(waits) if waits else 0.0
        return SimResult(jobs=completed, makespan=max(0.0, makespan), per_project_wait=per_project)
