from __future__ import annotations

import copy
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent.memory import PersonalMemoryStore
from backend.agent.task_scheduler import Job, PriorityScheduler, ProjectPolicy

ASSETS = ROOT / "docs" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

PURPLE = "#6f7cff"
ORANGE = "#f0a45b"
GREEN = "#55a97a"
INK = "#26324a"
GRID = "#e6eaf5"


def build_jobs(seed: int = 42) -> tuple[list[Job], list[ProjectPolicy]]:
    rng = np.random.default_rng(seed)
    projects = [
        ProjectPolicy("core", priority=5, weight=0.50),
        ProjectPolicy("growth", priority=3, weight=0.30),
        ProjectPolicy("debt", priority=1, weight=0.20),
    ]
    project_ids = [p.project_id for p in projects]
    probabilities = [0.50, 0.30, 0.20]

    jobs: list[Job] = []
    now = 0.0
    for index in range(150):
        now += float(rng.exponential(7.5))
        project_id = str(rng.choice(project_ids, p=probabilities))
        if project_id == "core":
            urgency = int(rng.choice([2, 3, 4], p=[0.15, 0.45, 0.40]))
        elif project_id == "growth":
            urgency = int(rng.choice([1, 2, 3], p=[0.2, 0.5, 0.3]))
        else:
            urgency = int(rng.choice([1, 2], p=[0.6, 0.4]))
        duration = float(rng.lognormal(mean=3.3, sigma=0.45))
        jobs.append(
            Job(
                id=f"task-{index:03d}",
                project_id=project_id,
                urgency=urgency,
                created_at=now,
                duration=duration,
            )
        )
    return jobs, projects


def scheduling_report() -> dict:
    jobs, projects = build_jobs()
    scheduler = PriorityScheduler(projects, max_workers=4, aging_per_minute=1.5)
    priority = scheduler.simulate(copy.deepcopy(jobs))
    fifo = scheduler.simulate_fifo(copy.deepcopy(jobs))

    def project_stats(result, project_id: str) -> dict:
        selected = [job for job in result.jobs if job.project_id == project_id]
        waits = [job.wait for job in selected]
        return {
            "count": len(selected),
            "avg_wait": float(np.mean(waits)) if waits else 0.0,
            "p95_wait": float(np.percentile(waits, 95)) if waits else 0.0,
            "max_wait": float(np.max(waits)) if waits else 0.0,
        }

    def urgent_stats(result) -> dict:
        selected = [job for job in result.jobs if job.urgency >= 3]
        waits = [job.wait for job in selected]
        return {
            "count": len(selected),
            "avg_wait": float(np.mean(waits)) if waits else 0.0,
            "p95_wait": float(np.percentile(waits, 95)) if waits else 0.0,
            "max_wait": float(np.max(waits)) if waits else 0.0,
        }

    report = {
        "priority": priority.summary(),
        "fifo": fifo.summary(),
        "urgent": {"priority": urgent_stats(priority), "fifo": urgent_stats(fifo)},
        "per_project": {
            project.project_id: {
                "priority": project_stats(priority, project.project_id),
                "fifo": project_stats(fifo, project.project_id),
                "policy_priority": project.priority,
                "policy_weight": project.weight,
            }
            for project in projects
        },
        "quotas": scheduler.quotas,
    }
    (ASSETS / "scheduling_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=180)
    for result, label, color in [(fifo, "FIFO", ORANGE), (priority, "优先级 + 配额 + 老化", PURPLE)]:
        waits = np.sort(np.array([job.wait for job in result.jobs if job.urgency >= 3]))
        cumulative = np.arange(1, len(waits) + 1) / len(waits)
        ax.step(waits, cumulative, where="post", label=label, color=color, linewidth=2.4)
    ax.set_xlabel("紧急任务等待时间（秒）")
    ax.set_ylabel("累计占比")
    ax.set_title("紧急任务等待时间 CDF：高优先级任务更快进入执行", loc="left", fontsize=13, color=INK)
    ax.grid(color=GRID, linewidth=0.8)
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(ASSETS / "scheduling_wait_cdf.png", bbox_inches="tight")
    plt.close(fig)

    labels = ["紧急任务\n平均等待", "紧急任务\nP95", "核心项目\n平均等待"]
    fifo_values = [
        report["urgent"]["fifo"]["avg_wait"],
        report["urgent"]["fifo"]["p95_wait"],
        report["per_project"]["core"]["fifo"]["avg_wait"],
    ]
    priority_values = [
        report["urgent"]["priority"]["avg_wait"],
        report["urgent"]["priority"]["p95_wait"],
        report["per_project"]["core"]["priority"]["avg_wait"],
    ]
    x = np.arange(len(labels))
    width = 0.34
    fig, ax = plt.subplots(figsize=(8.6, 4.4), dpi=180)
    bars1 = ax.bar(x - width / 2, fifo_values, width, label="FIFO", color=ORANGE, alpha=0.9)
    bars2 = ax.bar(x + width / 2, priority_values, width, label="优先级调度", color=PURPLE, alpha=0.9)
    ax.set_xticks(x, labels)
    ax.set_ylabel("秒")
    ax.set_title("紧急任务与核心项目：等待时间显著下降", loc="left", fontsize=13, color=INK)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    for bars in (bars1, bars2):
        for bar in bars:
            ax.annotate(
                f"{bar.get_height():.0f}",
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                ha="center",
                va="bottom",
                fontsize=8,
                color=INK,
            )
    ax.text(
        0.99, 0.95,
        f"吞吐：FIFO {report['fifo']['throughput_per_min']:.2f} / 优先级 {report['priority']['throughput_per_min']:.2f} 任务/分钟",
        transform=ax.transAxes, ha="right", va="top", fontsize=8.5, color="#6b7280",
    )
    fig.tight_layout()
    fig.savefig(ASSETS / "scheduling_metrics.png", bbox_inches="tight")
    plt.close(fig)

    names = {"core": "核心平台", "growth": "增长实验", "debt": "技术债"}
    ids = ["core", "growth", "debt"]
    x = np.arange(len(ids))
    fifo_waits = [report["per_project"][pid]["fifo"]["avg_wait"] for pid in ids]
    priority_waits = [report["per_project"][pid]["priority"]["avg_wait"] for pid in ids]
    fig, ax = plt.subplots(figsize=(8.2, 4.2), dpi=180)
    ax.bar(x - width / 2, fifo_waits, width, label="FIFO", color=ORANGE, alpha=0.9)
    ax.bar(x + width / 2, priority_waits, width, label="优先级调度", color=PURPLE, alpha=0.9)
    ax.set_xticks(x, [names[pid] for pid in ids])
    ax.set_ylabel("平均等待（秒）")
    ax.set_title("高优先级项目等待下降；低优先级以等待换产能，由老化兜底", loc="left", fontsize=13, color=INK)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(ASSETS / "scheduling_projects.png", bbox_inches="tight")
    plt.close(fig)

    return report


def relationship_report() -> dict:
    rng = np.random.default_rng(7)
    store = PersonalMemoryStore()
    groups = {
        "产品群": ["产品", "设计", "前端", "后端", "测试", "运营"],
        "算法群": ["算法", "数据", "平台", "产品"],
        "值班群": ["运维", "后端", "测试", "安全"],
        "增长群": ["增长", "运营", "数据", "设计"],
        "开源群": ["开源", "前端", "后端", "文档"],
        "项目群": ["项目经理", "产品", "前端", "后端", "测试", "算法"],
    }
    people: dict[str, str] = {}
    for group, members in groups.items():
        for name in members:
            user_id = f"u-{name}"
            people[user_id] = name
            store.add_person(user_id, name)

    group_threads = {group: f"thread-{index}" for index, group in enumerate(groups)}
    message_no = 0
    for group, members in groups.items():
        thread_id = group_threads[group]
        member_ids = [f"u-{name}" for name in members]
        # 每个群有一个高频核心成员，制造明显的关系中心
        weights = np.array([3.0 if i == 0 else 1.0 for i in range(len(member_ids))])
        weights = weights / weights.sum()
        for _ in range(120):
            author = str(rng.choice(member_ids, p=weights))
            message_no += 1
            store.record_message(
                message_id=f"m-{message_no:04d}",
                thread_id=thread_id,
                author_id=author,
                author_name=people[author],
                content=f"{group} 的消息 {message_no}",
                ensure_person=False,
            )

    for index, name in enumerate(["产品", "算法", "后端", "运维", "增长", "开源"]):
        store.remember(
            f"u-{name}",
            f"{name} 偏好结构化、先结论后细节的回复",
            kind="preference",
            scope="private",
            tags="表达偏好,汇报",
            importance=4,
        )

    graph = store.relationship_graph(window=4, min_weight=1)
    communities = list(nx.community.greedy_modularity_communities(graph))
    color_map = [PURPLE, ORANGE, GREEN, "#d9789c", "#7fb6d9", "#b58ad9"]
    node_colors = {}
    for index, community in enumerate(communities):
        for node in community:
            node_colors[node] = color_map[index % len(color_map)]

    fig, ax = plt.subplots(figsize=(8.6, 6.2), dpi=180)
    pos = nx.spring_layout(graph, seed=11, k=0.55, iterations=120)
    sizes = [360 + graph.nodes[node].get("messages", 0) * 2.2 for node in graph.nodes]
    widths = [0.5 + graph[u][v].get("weight", 1) * 0.09 for u, v in graph.edges]
    nx.draw_networkx_edges(graph, pos, ax=ax, width=widths, alpha=0.28, edge_color="#9aa7c7")
    nx.draw_networkx_nodes(
        graph, pos, ax=ax, node_size=sizes,
        node_color=[node_colors.get(node, "#c9d2ea") for node in graph.nodes],
        linewidths=1.2, edgecolors="white",
    )
    nx.draw_networkx_labels(
        graph, pos, ax=ax,
        labels={node: graph.nodes[node]["nickname"] for node in graph.nodes},
        font_size=8, font_family="Microsoft YaHei", font_color=INK,
    )
    ax.set_title("群聊关系图：节点=成员，边=同群/相邻发言的共现强度", loc="left", fontsize=13, color=INK)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(ASSETS / "relationship_graph.png", bbox_inches="tight")
    plt.close(fig)

    metrics = {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "density": round(nx.density(graph), 4),
        "avg_degree": round(sum(dict(graph.degree()).values()) / graph.number_of_nodes(), 2),
        "communities": len(communities),
        "top_relationships": store.top_relationships(8),
        "memory_example": [
            {"user": memory.user_id, "content": memory.content, "importance": memory.importance}
            for memory in store.list_memories("u-产品")
        ],
    }
    (ASSETS / "relationship_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


if __name__ == "__main__":
    scheduling = scheduling_report()
    relationships = relationship_report()
    print(json.dumps({"scheduling": scheduling, "relationships": relationships}, ensure_ascii=False, indent=2))
    print(f"charts -> {ASSETS}")
