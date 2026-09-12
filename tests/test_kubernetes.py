"""Kubernetes runtime command construction and isolation guardrails."""

import json
import subprocess

import pytest

from task_bundle.container import Kubernetes
from task_bundle.errors import DockerError


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_kubernetes_requires_deny_egress_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter([_ok(), subprocess.CompletedProcess([], 1, "", "not found")])
    monkeypatch.setattr("task_bundle.container._kubectl", lambda *args, **kwargs: next(replies))

    with pytest.raises(DockerError, match="NetworkPolicy"):
        Kubernetes(namespace="eval").ensure_available()


def test_kubernetes_pod_is_ephemeral_non_root_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_kubectl(args, *, timeout=None, stdin=None):
        calls.append((args, stdin))
        return _ok()

    monkeypatch.setattr("task_bundle.container._kubectl", fake_kubectl)
    runtime = Kubernetes(namespace="eval")

    pod = runtime.run_detached("registry/task:sha")

    assert pod.startswith("task-bundle-")
    manifest = json.loads(calls[0][1] or "{}")
    assert manifest["metadata"]["namespace"] == "eval"
    assert manifest["metadata"]["labels"]["task-bundle/network"] == "off"
    assert manifest["spec"]["restartPolicy"] == "Never"
    assert manifest["spec"]["securityContext"]["runAsUser"] == 1000
    container = manifest["spec"]["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    assert calls[1][0][0] == "wait"


def test_kubernetes_remove_is_nonblocking(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "task_bundle.container._kubectl",
        lambda args, **kwargs: (calls.append(args), _ok())[1],
    )

    Kubernetes(namespace="eval").rm_force("pod-x")

    assert calls == [
        [
            "delete",
            "pod",
            "pod-x",
            "-n",
            "eval",
            "--ignore-not-found=true",
            "--wait=false",
        ]
    ]
