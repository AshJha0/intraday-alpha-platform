#!/usr/bin/env python3
"""Regenerate the GENERATED ConfigMap manifests in deployment/k8s/ from their
sources of truth. Run whenever configs/, deployment/prometheus/ or
deployment/grafana/ change, then commit the results:

    python3 deployment/k8s/generate_configmaps.py

Outputs (do not hand-edit):
  configmap-configs.yaml     <- configs/*.json + configs/strategies/*.json
  configmap-prometheus.yaml  <- deployment/prometheus/{prometheus,recording,alerts}.yml
  configmap-grafana.yaml     <- deployment/grafana/provisioning + dashboards

Equivalent to `kubectl create configmap ... --from-file=... --dry-run=client
-o yaml`, but deterministic (sorted keys, stable ordering) and usable without
kubectl. Requires PyYAML.
"""
from __future__ import annotations

import argparse
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
    try:
        shown = out.relative_to(REPO)
    except ValueError:
        shown = out
    print(f"wrote {shown} ({len(body)} bytes)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir", type=Path, default=K8S,
        help="write the manifests here instead of deployment/k8s (used by "
             "tests/harness/check_deployment.py to diff against the committed "
             "files without touching the working tree)")
    args = ap.parse_args()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Platform configs (mounted at /etc/iap/configs).
    cfg_files = {p.name: p for p in sorted((REPO / "configs").glob("*.json"))}
    for p in sorted((REPO / "configs" / "strategies").glob("*.json")):
        cfg_files[f"strategies__{p.name}"] = p  # '/' not allowed in keys
    write(
        out_dir / "configmap-configs.yaml",
        "configs/*.json and configs/strategies/*.json",
        configmap("iap-configs", cfg_files),
    )

    # 2. Prometheus config + rules + file-SD targets (mounted at /etc/prometheus).
    prom = REPO / "deployment" / "prometheus"
    write(
        out_dir / "configmap-prometheus.yaml",
        "deployment/prometheus/",
        configmap(
            "iap-prometheus-config",
            {
                "prometheus.yml": prom / "prometheus.yml",
                "recording.yml": prom / "recording.yml",
                "alerts.yml": prom / "alerts.yml",
                # NOTE: the rust-telemetry file-SD target list was removed in
                # round 3 — no component ever wrote /data/telemetry/rust.prom,
                # so the target was permanently down and TargetDown (critical)
                # fired continuously. A scrape target is only added here
                # together with a producer (PLATFORM_CONVENTIONS.md §12.7).
            },
        ),
    )

    # 3. Grafana provisioning + dashboards.
    graf = REPO / "deployment" / "grafana"
    write(
        out_dir / "configmap-grafana.yaml",
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
