#!/usr/bin/env python3
"""Regenerate the GENERATED ConfigMap manifests in deployment/k8s/ from their
sources of truth. Run whenever configs/, deployment/prometheus/ or
deployment/grafana/ change, then commit the results:

    python3 deployment/k8s/generate_configmaps.py

Outputs (do not hand-edit):
  configmap-configs.yaml     <- configs/*.json + configs/strategies/*.json
  configmap-prometheus.yaml  <- deployment/prometheus/{prometheus,recording,alerts}.yml
                                + targets/rust-telemetry.json
  configmap-grafana.yaml     <- deployment/grafana/provisioning + dashboards

Equivalent to `kubectl create configmap ... --from-file=... --dry-run=client
-o yaml`, but deterministic (sorted keys, stable ordering) and usable without
kubectl. Requires PyYAML.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parent
REPO = K8S.parents[1]

HEADER = (
    "# GENERATED FILE — do not edit by hand.\n"
    "# Source of truth: {src}\n"
    "# Regenerate: python3 deployment/k8s/generate_configmaps.py\n"
)

LABELS = {
    "app.kubernetes.io/part-of": "intraday-alpha-platform",
    "app.kubernetes.io/component": "config",
    "app.kubernetes.io/managed-by": "generate-configmaps",
}


class LiteralStr(str):
    """Force YAML block-literal style for file bodies."""


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(LiteralStr, _literal_representer)


def configmap(name: str, files: dict[str, Path]) -> dict:
    data = {}
    for key in sorted(files):
        text = files[key].read_text()
        if not text.endswith("\n"):
            text += "\n"
        data[key] = LiteralStr(text)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": "intraday-alpha",
            "labels": dict(LABELS),
        },
        "data": data,
    }


def write(out: Path, src_desc: str, *manifests: dict) -> None:
    body = "---\n".join(
        yaml.dump(m, default_flow_style=False, sort_keys=False, width=100)
        for m in manifests
    )
    out.write_text(HEADER.format(src=src_desc) + body)
    print(f"wrote {out.relative_to(REPO)} ({len(body)} bytes)")


def main() -> int:
    # 1. Platform configs (mounted at /etc/iap/configs).
    cfg_files = {p.name: p for p in sorted((REPO / "configs").glob("*.json"))}
    for p in sorted((REPO / "configs" / "strategies").glob("*.json")):
        cfg_files[f"strategies__{p.name}"] = p  # '/' not allowed in keys
    write(
        K8S / "configmap-configs.yaml",
        "configs/*.json and configs/strategies/*.json",
        configmap("iap-configs", cfg_files),
    )

    # 2. Prometheus config + rules + file-SD targets (mounted at /etc/prometheus).
    prom = REPO / "deployment" / "prometheus"
    write(
        K8S / "configmap-prometheus.yaml",
        "deployment/prometheus/",
        configmap(
            "iap-prometheus-config",
            {
                "prometheus.yml": prom / "prometheus.yml",
                "recording.yml": prom / "recording.yml",
                "alerts.yml": prom / "alerts.yml",
                # NOTE: file-SD target list. In k8s the rust exporter is not
                # deployed yet (compose-only today); Prometheus reports the
                # target down until that wave, which is intended (TargetDown
                # alert documents the gap). Mounted flat at targets/ via k8s
                # subPath-free mount: prometheus.yml references
                # /etc/prometheus/targets/rust-telemetry.json, so the
                # deployment mounts the whole ConfigMap at /etc/prometheus and
                # this key must keep the 'targets__' prefix mapping below.
                "targets__rust-telemetry.json": prom / "targets" / "rust-telemetry.json",
            },
        ),
    )

    # 3. Grafana provisioning + dashboards.
    graf = REPO / "deployment" / "grafana"
    write(
        K8S / "configmap-grafana.yaml",
        "deployment/grafana/ (provisioning + dashboards)",
        configmap(
            "iap-grafana-provisioning",
            {
                "datasources-prometheus.yml": graf / "provisioning" / "datasources" / "prometheus.yml",
                "dashboards-provider.yml": graf / "provisioning" / "dashboards" / "dashboards.yml",
            },
        ),
        configmap(
            "iap-grafana-dashboards",
            {p.name: p for p in sorted((graf / "dashboards").glob("*.json"))},
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
