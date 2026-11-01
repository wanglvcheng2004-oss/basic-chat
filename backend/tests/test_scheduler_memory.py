from __future__ import annotations

from backend.agent.memory import PersonalMemoryStore
from backend.agent.task_scheduler import Job, PriorityScheduler, ProjectPolicy


def test_priority_scheduler_prefers_high_priority_project():
    scheduler = PriorityScheduler(
        [ProjectPolicy("high", priority=5), ProjectPolicy("low", priority=1)],
        max_workers=1,
        aging_per_minute=0.0,
    )
    jobs = [
        Job("low-1", "low", urgency=2, created_at=0, duration=60),
        Job("high-1", "high", urgency=2, created_at=0, duration=60),
    ]
    result = scheduler.simulate(jobs)
    started = sorted(result.jobs, key=lambda job: job.started_at or 0)
    assert started[0].id == "high-1"


def test_weighted_quota_reserves_capacity_for_other_project():
    scheduler = PriorityScheduler(
        [ProjectPolicy("a", priority=3, weight=1), ProjectPolicy("b", priority=3, weight=1)],
        max_workers=2,
        aging_per_minute=0.0,
    )
    jobs = [
        Job("a-1", "a", urgency=2, created_at=0, duration=120),
        Job("a-2", "a", urgency=2, created_at=0, duration=120),
        Job("b-1", "b", urgency=1, created_at=0, duration=120),
    ]
    result = scheduler.simulate(jobs)
    running_at_zero = [job.id for job in result.jobs if job.started_at == 0]
    assert "a-1" in running_at_zero
    assert "b-1" in running_at_zero
    assert "a-2" not in running_at_zero


def test_memory_is_isolated_per_user():
    store = PersonalMemoryStore()
    store.add_person("alice", "Alice")
    store.add_person("bob", "Bob")
    store.remember("alice", "偏好简洁回复", kind="preference", tags="风格", importance=5)
    assert [memory.content for memory in store.recall("alice", "简洁")] == ["偏好简洁回复"]
    assert store.recall("bob") == []


def test_memory_query_and_forget():
    store = PersonalMemoryStore()
    store.remember("alice", "项目使用 pnpm", scope="project:p1", tags="工具链")
    store.remember("alice", "喜欢深色主题", tags="界面")
    assert len(store.recall("alice", "pnpm", scope="project:p1")) == 1
    memory_id = store.recall("alice", "深色")[0].id
    assert store.forget("alice", memory_id) is True
    assert store.recall("alice", "深色") == []


def test_relationship_graph_links_thread_participants():
    store = PersonalMemoryStore()
    store.add_person("alice", "Alice")
    store.add_person("bob", "Bob")
    store.record_message(
        message_id="m1", thread_id="t1", author_id="alice",
        author_name="Alice", content="hi", ensure_person=False,
    )
    store.record_message(
        message_id="m2", thread_id="t1", author_id="bob",
        author_name="Bob", content="hello", ensure_person=False,
    )
    graph = store.relationship_graph()
    assert graph.number_of_nodes() == 2
    assert graph.has_edge("alice", "bob")
    assert graph["alice"]["bob"]["weight"] >= 1
