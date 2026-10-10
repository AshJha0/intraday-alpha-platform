#!/usr/bin/env python3
"""tests/harness/check_deployment.py — structural validation of deployment/.

GOVERNANCE.md §1 promises "YAML must pass the structural validation used in
CI"; PLATFORM_CONVENTIONS.md §12.7 pins what that means. This script IS that
validation. It is run by `tests/harness/run_all.sh` and by
`.github/workflows/ci.yml`, and it needs nothing but Python + PyYAML; every
external validator (promtool, docker compose, kubectl/kubeconform) is used
when present and reported as SKIP when not — a skip is printed loudly, never
silently passed off as a pass.

Checks (each one is a named case; exit code 0 iff no case FAILED):

  prometheus_rules_load       promtool check rules   (or a strict YAML +
                              Go-template-syntax fallback parser)
  prometheus_config           promtool check config, with rule_files staged so
                              the /etc/prometheus paths really resolve
  prometheus_rule_unit_tests  promtool test rules deployment/prometheus/tests/
  alert_metric_provenance     every metric referenced by a rule is exported by
                              a real producer (PLATFORM_CONVENTIONS.md §12.6)
  alert_annotation_functions  annotations use only Go text/template +
                              Prometheus functions (no Sprig `default`)
  compose_config              docker compose config -q
  compose_log_limits          every long-running service caps its log files
  compose_and_dockerfile_env  IAP_CONFIG_DIR is honoured end to end
  dockerfile_copy_sources     every COPY source exists and is not excluded by
                              .dockerignore
  docker_build_context        tests/harness/check_docker_build.py — a clean
                              checkout of the tracked tree (git ls-files, so
                              staged moves count) + .dockerignore reproduces
                              the real build context, every stage's COPY and every
                              build/runtime input resolves in it, and the Java
                              build stage actually runs (a real `docker build`
                              when a daemon is reachable)
  dockerignore_present        the build context excludes host build trees
  k8s_manifests_parse         every manifest is valid YAML with apiVersion/kind
  k8s_dry_run                 kubectl apply --dry-run=client (or kubeconform)
  k8s_trading_singleton       replicas 1 + Recreate + probes that can fail
  configmaps_in_sync          generate_configmaps.py output == committed files
  configmap_items_in_sync     every volume that projects the iap-configs
                              ConfigMap lists exactly the generator's
                              key -> path items, so the pod sees the nested
                              configs/<domain>/ tree at IAP_CONFIG_DIR
  dashboards_valid            dashboard JSON parses, datasource uid stable, and
                              every panel expression names a real metric
  java_golden_gate_complete   JAVA_GOLDEN_CLASSES == the *GoldenTest.java set
  alerting_wired              Prometheus -> Alertmanager in config, compose and
                              k8s, plus the always-firing Watchdog rule
  k8s_pod_hardening           automountServiceAccountToken false everywhere,
                              java root fs read-only with a /tmp emptyDir, its
                              own state PVC, explicit storageClassName on every
                              PVC, IAP_BIND_ADDR set
  k8s_network_policy          default-deny ingress AND egress, DNS egress, the
                              operator -> java admin ingress rule
  compose_exposure            8080/9090 published on 127.0.0.1 only
  image_pinning               every Dockerfile FROM and every third-party
                              compose/k8s image carries a sha256 digest; iap
                              images use the ghcr names with one version tag
  workflow_supply_chain       every `uses:` in .github/workflows is a full
                              commit SHA, runners are pinned, every cargo
                              invocation is --locked, permissions are declared
  rust_toolchain_pinned       rust-toolchain.toml == ci.yml == Dockerfile.rust
  alerting_secret_free        no receiver URL, token or Secret value is
                              committed: alertmanager.yml uses url_file only,
                              files under deployment/alertmanager/ hold only a
                              local (compose-internal) URL, no k8s Secret
                              manifest carries data (v1.12 P4)
  alert_rule_metadata         every alert carries severity (page | critical |
                              warning | none) and runbook + runbook_url
                              annotations that resolve to a file in the repo
  alert_rule_tests            every rule file is loaded by a promtool test and
                              every alert is exercised by name in one (minus a
                              shrinking, explicit legacy allowlist)
  k8s_state_backup            the state-backup CronJob reads iap-java-state
                              READ-ONLY, is pinned to the java-platform node
                              by required pod affinity, never overlaps itself,
                              and writes to its own claim
  release_guard               release.yml verifies the commit with
                              tests/harness/verify_ci_green.py, its manual
                              trigger is a dry run (a `dry_run` and a `sha`
                              input) and the image and manifest jobs run on
                              tag pushes only

Usage:
    python3 tests/harness/check_deployment.py [--verbose]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deployment"
PROM = DEPLOY / "prometheus"

VERBOSE = "--verbose" in sys.argv

RESULTS: list[tuple[str, str, str]] = []  # (case, status, detail)


def record(case: str, status: str, detail: str = "") -> None:
    RESULTS.append((case, status, detail))
    mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[status]
    print(f"  [{mark}] {case}{': ' + detail if detail else ''}")


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------- metrics ---
# The metrics the deployment actually exports (PLATFORM_CONVENTIONS.md §12.6).
# Sourced from com.iap.platform.PaperTrading + com.iap.platform.PaperTraces +
# com.iap.risk.RiskEngine + com.iap.monitoring.GcMetrics; `up` is Prometheus's own.
EXPORTED_METRICS = {
    # market data
    "md_events_total",
    "md_sequence_gaps_total",
    "md_duplicates_total",
    "md_last_event_unixtime",
    "md_last_event_wallclock_unixtime",
    "md_event_time_gap_seconds",
    "book_stale",
    # platform lifecycle
    "platform_mode",
    "platform_session_state",
    # last persisted session state, served by com.iap.platform.SessionStateExporter
    # (the java-platform-state sidecar / compose service) — outlives the JVM
    "platform_persisted_session_state",
    "platform_persisted_session_unixtime",
    "platform_persisted_session_event_cursor",
    "risk_session_restarts_total",
    "admin_requests_total",
    # safety counters of the 2026-10-03 paper-platform review (§12.6); every
    # one is created on first use, so a rule must not rely on a zero sample
    "risk_routed_venue_mismatch_total",
    "risk_resume_open_orders_released_total",
    "exec_orders_blocked_kill_pending_total",
    "admin_auth_rate_limited_total",
    "admin_audit_suppressed_total",
    # latency histograms
    "decode_latency_ns",
    "book_update_latency_ns",
    "order_path_latency_ns",
    "exec_slippage_bps",
    "jvm_gc_pause_ns",
    # alpha / portfolio
    "alpha_signals_total",
    "alpha_live_vs_backtest_drift",
    "alpha_rolling_ic",
    "alpha_lifecycle_state",
    "portfolio_solves_total",
    "portfolio_gross_notional",
    "portfolio_net_notional",
    "portfolio_drawdown",
    # execution
    "exec_orders_submitted_total",
    "exec_fills_total",
    "exec_child_orders_rejected_total",
    # risk
    "risk_events_total",
    "risk_decisions_total",
    "risk_allowed_total",
    "risk_rejected_total",
    "risk_realized_pnl",
    "risk_unrealized_pnl",
    "risk_daily_pnl",
    "risk_kill_switch_engaged",
    "risk_limit",
    # decision trace (com.iap.platform.PaperTraces, 2026-09-19; the names
    # rust/telemetry::trace pins) — exported, not yet on a panel (EPICS O06)
    "trace_records_total",
    "trace_tca_skipped_total",
    # prometheus itself
    "up",
}

# Go text/template builtins plus Prometheus's own template functions.
ALLOWED_TEMPLATE_FUNCS = {
    "and",
    "call",
    "html",
    "index",
    "slice",
    "js",
    "len",
    "not",
    "or",
    "print",
    "printf",
    "println",
    "urlquery",
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "if",
    "else",
    "end",
    "range",
    "with",
    "template",
    "block",
    "define",
    "humanize",
    "humanize1024",
    "humanizeDuration",
    "humanizePercentage",
    "humanizeTimestamp",
    "title",
    "toUpper",
    "toLower",
    "match",
    "reReplaceAll",
    "graphLink",
    "tableLink",
    "parseDuration",
    "stripPort",
    "stripDomain",
    "toTime",
    "pathPrefix",
    "externalURL",
    "value",
    "args",
    "safeHtml",
    "sortByLabel",
    "first",
    "label",
    "strvalue",
    "query",
}

RULE_FILES = [PROM / "alerts.yml", PROM / "recording.yml", PROM / "slo.yml"]


def rule_docs() -> list[dict]:
    out = []
    for f in RULE_FILES:
        out.append(yaml.safe_load(f.read_text()))
    return out


def all_rules() -> list[dict]:
    rules = []
    for doc in rule_docs():
        for group in doc.get("groups", []):
            rules.extend(group.get("rules", []))
    return rules


# ------------------------------------------------------------ prometheus ---
def check_prometheus_rules() -> None:
    if have("promtool"):
        rc, out = run(["promtool", "check", "rules", *map(str, RULE_FILES)])
        record("prometheus_rules_load", "PASS" if rc == 0 else "FAIL", "" if rc == 0 else out)
        return
    # Fallback: strict YAML + Go-template syntax + rule-shape validation.
    problems = []
    for f in RULE_FILES:
        try:
            doc = yaml.safe_load(f.read_text())
        except yaml.YAMLError as exc:
            problems.append(f"{f.name}: {exc}")
            continue
        if not isinstance(doc, dict) or "groups" not in doc:
            problems.append(f"{f.name}: no top-level groups")
            continue
        for group in doc["groups"]:
            if "name" not in group:
                problems.append(f"{f.name}: group without a name")
            for rule in group.get("rules", []):
                if "expr" not in rule:
                    problems.append(f"{f.name}: rule without expr: {rule}")
                if "alert" not in rule and "record" not in rule:
                    problems.append(f"{f.name}: rule is neither alert nor record")
                problems += template_problems(rule, f.name)
    record(
        "prometheus_rules_load",
        "PASS" if not problems else "FAIL",
        "promtool absent, used the strict fallback parser"
        if not problems
        else "; ".join(problems[:5]),
    )


def template_problems(rule: dict, where: str) -> list[str]:
    """Every {{ ... }} action must balance and use a known function."""
    problems = []
    for field, text in list(rule.get("annotations", {}).items()) + list(
        rule.get("labels", {}).items()
    ):
        if not isinstance(text, str):
            continue
        if text.count("{{") != text.count("}}"):
            problems.append(f"{where} {rule.get('alert')}.{field}: unbalanced {{{{ }}}}")
        for action in re.findall(r"\{\{(.*?)\}\}", text, re.S):
            # tokens after a pipe are functions; a leading token that is not a
            # variable/literal is a function too.
            for segment in action.split("|")[1:]:
                name = segment.strip().split(" ")[0]
                if name and name not in ALLOWED_TEMPLATE_FUNCS:
                    problems.append(
                        f'{where} {rule.get("alert")}.{field}: function "{name}" not defined'
                    )
    return problems


def check_annotation_functions() -> None:
    problems = []
    for rule in all_rules():
        problems += template_problems(rule, "alerts.yml")
    record(
        "alert_annotation_functions", "PASS" if not problems else "FAIL", "; ".join(problems[:5])
    )


def check_prometheus_config() -> None:
    cfg = PROM / "prometheus.yml"
    if not have("promtool"):
        try:
            doc = yaml.safe_load(cfg.read_text())
            assert "scrape_configs" in doc and "rule_files" in doc
            record("prometheus_config", "PASS", "promtool absent, YAML only")
        except Exception as exc:  # noqa: BLE001
            record("prometheus_config", "FAIL", str(exc))
        return
    # Stage the rule files where the config says they live, so the FULL check
    # (including rule_files resolution) runs, not just --syntax-only.
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        doc = yaml.safe_load(cfg.read_text())
        staged_rules = []
        for ref in doc.get("rule_files", []):
            name = Path(ref).name
            src = PROM / name
            if not src.exists():
                record(
                    "prometheus_config",
                    "FAIL",
                    f"rule_files references {ref}, but {src} does not exist",
                )
                return
            (stage / name).write_text(src.read_text())
            staged_rules.append(str(stage / name))
        doc["rule_files"] = staged_rules
        staged_cfg = stage / "prometheus.yml"
        staged_cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
        rc, out = run(["promtool", "check", "config", str(staged_cfg)])
        record("prometheus_config", "PASS" if rc == 0 else "FAIL", "" if rc == 0 else out)


def check_prometheus_rule_tests() -> None:
    tests = sorted((PROM / "tests").glob("*.yml"))
    if not tests:
        record("prometheus_rule_unit_tests", "FAIL", "no rule unit tests")
        return
    if not have("promtool"):
        record("prometheus_rule_unit_tests", "SKIP", "promtool not installed")
        return
    for t in tests:
        rc, out = run(["promtool", "test", "rules", t.name], cwd=t.parent)
        if rc != 0:
            record("prometheus_rule_unit_tests", "FAIL", f"{t.name}: {out}")
            return
    record("prometheus_rule_unit_tests", "PASS", f"{len(tests)} file(s)")


METRIC_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
PROMQL_KEYWORDS = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "and",
    "or",
    "unless",
    "offset",
    "bool",
    "rate",
    "increase",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "clamp_min",
    "clamp_max",
    "histogram_quantile",
    "time",
    "vector",
    "absent",
    "last_over_time",
    "topk",
    "bottomk",
    "delta",
    "irate",
    "quantile",
    "le",
    "job",
    "service",
    "instance",
    "limit",
    "mode",
    "alpha",
    "instrument",
    "m",
    "h",
    "s",
    "d",
}


def referenced_metrics(expr: str) -> set[str]:
    """Metric-ish identifiers in a PromQL expression."""
    stripped = re.sub(r'"[^"]*"', "", expr)
    # Drop numeric literals first, exponent and duration suffixes included
    # (10e6, 1e-9, 5m, 0.5): otherwise "e6" and "m" look like metric names.
    stripped = re.sub(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[smhdwy]?\b", " ", stripped)
    names = set()
    for token in METRIC_RE.findall(stripped):
        if token in PROMQL_KEYWORDS or token.startswith("job:"):
            continue
        names.add(token)
    return names


def check_metric_provenance() -> None:
    """§12.7 — a rule may only reference a metric a producer exports."""
    recorded = {r["record"] for r in all_rules() if "record" in r}
    unknown = {}
    for rule in all_rules():
        name = rule.get("alert") or rule.get("record")
        for metric in referenced_metrics(rule["expr"]):
            base = metric
            for suffix in ("_bucket", "_sum", "_count"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            if base in EXPORTED_METRICS or metric in recorded:
                continue
            unknown.setdefault(metric, []).append(name)
    record(
        "alert_metric_provenance",
        "PASS" if not unknown else "FAIL",
        ""
        if not unknown
        else "no producer for "
        + ", ".join(f"{m} (used by {', '.join(v)})" for m, v in unknown.items()),
    )


# ---------------------------------------------------------------- docker ---
COMPOSE = DEPLOY / "docker" / "docker-compose.yml"


def compose_doc() -> dict:
    text = COMPOSE.read_text()
    # `${VAR:?msg}` is valid compose interpolation but not YAML-hostile; safe
    # to parse as a plain string.
    return yaml.safe_load(text)


def check_docker_build_context() -> None:
    script = ROOT / "tests" / "harness" / "check_docker_build.py"
    rc, out = run([sys.executable, str(script)])
    tail = [ln for ln in out.splitlines() if ln.startswith("docker build checks")]
    record("docker_build_context", "PASS" if rc == 0 else "FAIL", (tail[-1] if tail else out)[:300])


def check_compose_config() -> None:
    if not have("docker"):
        record("compose_config", "SKIP", "docker CLI not installed")
        return
    env = dict(os.environ)
    env.setdefault("GRAFANA_ADMIN_PASSWORD", "check-deployment-placeholder")
    proc = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=COMPOSE.parent,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    out = (proc.stdout + proc.stderr).strip()
    record("compose_config", "PASS" if proc.returncode == 0 else "FAIL", out)


def check_compose_log_limits() -> None:
    doc = compose_doc()
    bad = []
    for name, svc in doc.get("services", {}).items():
        restart = str(svc.get("restart", "no"))
        if restart in ("no", '"no"'):
            continue  # batch jobs: they exit and are not restarted
        opts = (svc.get("logging") or {}).get("options") or {}
        if "max-size" not in opts or "max-file" not in opts:
            bad.append(name)
    record(
        "compose_log_limits",
        "PASS" if not bad else "FAIL",
        "" if not bad else "no logging.options.max-size/max-file: " + ", ".join(bad),
    )


def check_config_dir_wiring() -> None:
    """§12.2 — IAP_CONFIG_DIR is honoured by the code, image and compose."""
    problems = []
    java = (
        ROOT / "java" / "src" / "main" / "java" / "com" / "iap" / "config" / "ConfigService.java"
    ).read_text()
    if "IAP_CONFIG_DIR" not in java:
        problems.append("no Java code reads IAP_CONFIG_DIR")
    dockerfile = (DEPLOY / "docker" / "Dockerfile.java").read_text()
    if "IAP_CONFIG_DIR" not in dockerfile:
        problems.append("Dockerfile.java does not pass IAP_CONFIG_DIR")
    doc = compose_doc()
    jp = doc["services"].get("java-platform", {})
    env = jp.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env if "=" in e)
    if "IAP_CONFIG_DIR" not in env:
        problems.append("compose java-platform sets no IAP_CONFIG_DIR")
    mounts = [str(v) for v in jp.get("volumes", [])]
    if not any("configs" in m and m.endswith(":ro") for m in mounts):
        problems.append("compose java-platform does not bind-mount configs:ro")
    k8s = (DEPLOY / "k8s" / "java-platform.yaml").read_text()
    if "IAP_CONFIG_DIR" not in k8s:
        problems.append("k8s java-platform sets no IAP_CONFIG_DIR")
    record("compose_and_dockerfile_env", "PASS" if not problems else "FAIL", "; ".join(problems))


DOCKERIGNORE = ROOT / ".dockerignore"


def dockerignore_patterns() -> list[str]:
    if not DOCKERIGNORE.exists():
        return []
    return [
        ln.strip()
        for ln in DOCKERIGNORE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def check_dockerignore() -> None:
    if not DOCKERIGNORE.exists():
        record("dockerignore_present", "FAIL", "no .dockerignore at the repo root")
        return
    pats = dockerignore_patterns()
    required = ["cpp/build/", "rust/target/", "java/out/", ".git/"]
    missing = [r for r in required if r not in pats]
    record(
        "dockerignore_present",
        "PASS" if not missing else "FAIL",
        "" if not missing else "missing patterns: " + ", ".join(missing),
    )


COPY_RE = re.compile(r"^COPY\s+(?:--from=\S+\s+)?(.+)$", re.M)


def check_dockerfile_copy_sources() -> None:
    """Every `COPY <src>` from the build context must exist and be shippable."""
    problems = []
    pats = dockerignore_patterns()
    for df in sorted((DEPLOY / "docker").glob("Dockerfile.*")):
        text = df.read_text()
        # join line continuations
        text = re.sub(r"\\\n\s*", " ", text)
        for m in COPY_RE.finditer(text):
            line = m.group(0)
            args = m.group(1).split()
            if len(args) < 2:
                problems.append(f"{df.name}: malformed COPY: {line}")
                continue
            if "--from=" in line:
                continue  # a previous stage's filesystem, not the context
            for src in args[:-1]:
                path = ROOT / src
                if not path.exists():
                    problems.append(f"{df.name}: COPY source {src} does not exist")
                    continue
                for pat in pats:
                    p = pat.rstrip("/")
                    if src == p or src.startswith(p + "/"):
                        problems.append(
                            f"{df.name}: COPY {src} is excluded by .dockerignore pattern {pat}"
                        )
    record("dockerfile_copy_sources", "PASS" if not problems else "FAIL", "; ".join(problems[:5]))


# ------------------------------------------------------------------- k8s ---
K8S = DEPLOY / "k8s"


def k8s_docs() -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(K8S.glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc:
                out.append((f, doc))
    return out


def check_k8s_parse() -> None:
    problems = []
    try:
        docs = k8s_docs()
    except yaml.YAMLError as exc:
        record("k8s_manifests_parse", "FAIL", str(exc))
        return
    for f, doc in docs:
        for key in ("apiVersion", "kind", "metadata"):
            if key not in doc:
                problems.append(f"{f.name}: document without {key}")
    record(
        "k8s_manifests_parse",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:5]) or f"{len(docs)} documents",
    )


def check_k8s_dry_run() -> None:
    if have("kubeconform"):
        rc, out = run(
            ["kubeconform", "-strict", "-summary", *[str(p) for p in sorted(K8S.glob("*.yaml"))]]
        )
        record("k8s_dry_run", "PASS" if rc == 0 else "FAIL", out)
        return
    if have("kubectl"):
        rc, out = run(["kubectl", "apply", "--dry-run=client", "-f", str(K8S)])
        record("k8s_dry_run", "PASS" if rc == 0 else "FAIL", out)
        return
    record("k8s_dry_run", "SKIP", "neither kubeconform nor kubectl installed")


def check_k8s_singleton() -> None:
    """§12.7 — the trading vertical is a singleton with probes that can fail."""
    problems = []
    found = False
    for _f, doc in k8s_docs():
        if doc["kind"] == "PodDisruptionBudget" and doc["metadata"]["name"] == "java-platform":
            problems.append("a PDB on a single-replica singleton blocks drains")
        if doc["kind"] != "Deployment" or doc["metadata"]["name"] != "java-platform":
            continue
        found = True
        spec = doc["spec"]
        if spec.get("replicas") != 1:
            problems.append(f"replicas is {spec.get('replicas')}, must be 1")
        if (spec.get("strategy") or {}).get("type") != "Recreate":
            problems.append("strategy must be Recreate (no rollout overlap)")
        container = spec["template"]["spec"]["containers"][0]
        live = (container.get("livenessProbe") or {}).get("httpGet", {})
        ready = (container.get("readinessProbe") or {}).get("httpGet", {})
        if live.get("path") != "/health":
            problems.append("livenessProbe must GET /health")
        if ready.get("path") != "/ready":
            problems.append(
                "readinessProbe must GET /ready (not /metrics: a TCP bind is not readiness)"
            )
        if "startupProbe" not in container:
            problems.append("no startupProbe for the decode phase")
    if not found:
        problems.append("no java-platform Deployment found")
    record("k8s_trading_singleton", "PASS" if not problems else "FAIL", "; ".join(problems))


def check_configmaps_in_sync() -> None:
    gen = K8S / "generate_configmaps.py"
    committed = {p.name: p.read_text() for p in K8S.glob("configmap-*.yaml")}
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "k8s"
        stage.mkdir()
        rc, out = run([sys.executable, str(gen), "--out-dir", str(stage)])
        if rc != 0:
            record("configmaps_in_sync", "FAIL", out)
            return
        drift = []
        for name, text in sorted(committed.items()):
            produced = stage / name
            if not produced.exists():
                drift.append(f"{name}: not produced by the generator")
            elif produced.read_text() != text:
                drift.append(f"{name}: differs from the generator output")
    record(
        "configmaps_in_sync",
        "PASS" if not drift else "FAIL",
        "; ".join(drift) or f"{len(committed)} ConfigMaps",
    )


def check_configmap_items_in_sync() -> None:
    """The nested configs/ tree reaches the pod only through items[].path
    (a ConfigMap key cannot contain '/'): every consumer of iap-configs must
    list exactly the generator's key -> path pairs, or a moved/added config
    file silently vanishes from IAP_CONFIG_DIR."""
    sys.path.insert(0, str(K8S))
    try:
        import generate_configmaps  # noqa: WPS433 (local tool module)
    finally:
        sys.path.pop(0)
    want = [(i["key"], i["path"]) for i in generate_configmaps.configmap_items()]
    problems = []
    consumers = 0
    for f, doc in k8s_docs():
        for vol in iter_volumes(doc):
            cm = vol.get("configMap") or {}
            if cm.get("name") != "iap-configs":
                continue
            consumers += 1
            got = [(i.get("key"), i.get("path")) for i in cm.get("items") or []]
            if got != want:
                problems.append(
                    f"{f.name} volume {vol.get('name')!r}: items differ from "
                    f"generate_configmaps.configmap_items() "
                    f"(missing {sorted(set(want) - set(got))}, "
                    f"extra {sorted(set(got) - set(want))})"
                )
    if consumers == 0:
        problems.append("no manifest mounts the iap-configs ConfigMap")
    for key, path in want:
        if "/" in key or key.replace("__", "/") != path:
            problems.append(f"key {key!r} does not encode path {path!r}")
    record(
        "configmap_items_in_sync",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:5]) or f"{consumers} consumer(s) x {len(want)} files",
    )


def iter_volumes(doc: dict):
    """Pod-spec volumes of a Deployment / CronJob / Job / Pod document."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        kind = "Job"
    if kind in ("Deployment", "Job", "StatefulSet", "DaemonSet"):
        spec = (spec.get("template") or {}).get("spec") or {}
    elif kind != "Pod":
        return []
    return spec.get("volumes") or []


# -------------------------------------------------------------- grafana ----
def check_dashboards() -> None:
    problems = []
    recorded = {r["record"] for r in all_rules() if "record" in r}
    for f in sorted((DEPLOY / "grafana" / "dashboards").glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{f.name}: {exc}")
            continue
        if not doc.get("uid"):
            problems.append(f"{f.name}: no stable uid")
        for panel in doc.get("panels", []):
            for target in panel.get("targets", []):
                ds = (target.get("datasource") or {}).get("uid")
                if ds != "prometheus":
                    problems.append(f"{f.name}: panel {panel['id']} datasource uid {ds!r}")
                expr = target.get("expr", "")
                placeholder = "PLACEHOLDER" in panel.get("title", "")
                for metric in referenced_metrics(expr):
                    if metric in EXPORTED_METRICS or metric in recorded:
                        continue
                    if metric.rstrip("_bucket") in EXPORTED_METRICS:
                        continue
                    if placeholder:
                        continue
                    problems.append(
                        f"{f.name}: panel {panel['id']} "
                        f"({panel.get('title')}) uses {metric}, which no "
                        f"producer exports"
                    )
    record("dashboards_valid", "PASS" if not problems else "FAIL", "; ".join(problems[:5]))


# -------------------------------------------------------------- harness ----
def check_java_golden_gate() -> None:
    """§12.7 / round-3 SEV-2 — the Java golden gate must cover every class."""
    run_all = (ROOT / "tests" / "harness" / "run_all.sh").read_text()
    m = re.search(r'JAVA_GOLDEN_CLASSES="([^"]*)"', run_all)
    if not m:
        record("java_golden_gate_complete", "FAIL", "JAVA_GOLDEN_CLASSES not found in run_all.sh")
        return
    listed = {c.split(".")[-1] for c in m.group(1).split()}
    on_disk = {
        p.stem
        for p in (ROOT / "java" / "src" / "test" / "java" / "com" / "iap").glob("*GoldenTest.java")
    }
    missing = sorted(on_disk - listed)
    extra = sorted(listed - on_disk)
    detail = ""
    if missing:
        detail += "not in the golden gate: " + ", ".join(missing)
    if extra:
        detail += (" " if detail else "") + "listed but absent: " + ", ".join(extra)
    record(
        "java_golden_gate_complete",
        "PASS" if not (missing or extra) else "FAIL",
        detail or f"{len(on_disk)} golden classes",
    )


# ----------------------------------------------- governance hardening ------
WORKFLOWS = ROOT / ".github" / "workflows"


def pod_specs() -> list[tuple[str, str, dict]]:
    """(file, workload name, pod spec) for every workload in deployment/k8s."""
    out = []
    for f, doc in k8s_docs():
        kind = doc.get("kind")
        spec = doc.get("spec") or {}
        if kind == "CronJob":
            spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        if kind in ("Deployment", "CronJob", "Job", "StatefulSet", "DaemonSet"):
            pod = (spec.get("template") or {}).get("spec") or {}
            out.append((f.name, doc["metadata"]["name"], pod))
    return out


def check_alerting_wired() -> None:
    problems = []
    prom = yaml.safe_load((PROM / "prometheus.yml").read_text())
    targets = [
        t
        for am in (prom.get("alerting") or {}).get("alertmanagers", [])
        for sc in am.get("static_configs", [])
        for t in sc.get("targets", [])
    ]
    if "alertmanager:9093" not in targets:
        problems.append("prometheus.yml has no alerting.alertmanagers target alertmanager:9093")
    if "alertmanager" not in compose_doc().get("services", {}):
        problems.append("compose has no alertmanager service")
    kinds = {(d["kind"], d["metadata"]["name"]) for _, d in k8s_docs()}
    for want in [
        ("Deployment", "alertmanager"),
        ("Service", "alertmanager"),
        ("ConfigMap", "iap-alertmanager-config"),
    ]:
        if want not in kinds:
            problems.append(f"k8s is missing {want[0]}/{want[1]}")
    am_cfg = (DEPLOY / "alertmanager" / "alertmanager.yml").read_text()
    if "url_file" not in am_cfg or re.search(r"^\s*-?\s*url:\s", am_cfg, re.M):
        problems.append(
            "alertmanager.yml must read the webhook URL from url_file, never an inline url"
        )
    if not any(
        r.get("alert") == "Watchdog" and r.get("expr").strip() == "vector(1)" for r in all_rules()
    ):
        problems.append("no always-firing Watchdog rule (expr: vector(1))")
    record("alerting_wired", "PASS" if not problems else "FAIL", "; ".join(problems))


def check_k8s_pod_hardening() -> None:
    problems = []
    pods = pod_specs()
    for _fname, name, pod in pods:
        if pod.get("automountServiceAccountToken") is not False:
            problems.append(f"{name}: automountServiceAccountToken is not false")
    claims = {}
    for _fname, name, pod in pods:
        for vol in pod.get("volumes") or []:
            pvc = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if pvc:
                claims.setdefault(pvc, []).append(name)
    for pvc, users in claims.items():
        # A read-only, node-pinned reader (the state-backup CronJob) is the one
        # sanctioned exception; check_k8s_state_backup pins its shape.
        writers = set(users) - set(SANCTIONED_READERS.get(pvc, ()))
        if len(writers) > 1:
            problems.append(f"PVC {pvc} is shared by {sorted(writers)}")
    declared = {
        d["metadata"]["name"]: d for _, d in k8s_docs() if d["kind"] == "PersistentVolumeClaim"
    }
    for pvc in claims:
        if pvc not in declared:
            problems.append(f"workload claims {pvc}, which pvc.yaml does not declare")
    for pvc, doc in declared.items():
        if not (doc.get("spec") or {}).get("storageClassName"):
            problems.append(f"PVC {pvc} has no explicit storageClassName")
    java = [p for _, n, p in pods if n == "java-platform"]
    if not java:
        problems.append("no java-platform workload")
    else:
        pod = java[0]
        c = pod["containers"][0]
        if (c.get("securityContext") or {}).get("readOnlyRootFilesystem") is not True:
            problems.append("java-platform readOnlyRootFilesystem must be true")
        mounts = {m["mountPath"]: m["name"] for m in c.get("volumeMounts", [])}
        vols = {v["name"]: v for v in pod.get("volumes", [])}
        if "/tmp" not in mounts or "emptyDir" not in vols.get(mounts["/tmp"], {}):
            problems.append("java-platform needs an emptyDir mounted at /tmp")
        env = {e["name"]: e.get("value") for e in c.get("env", [])}
        if "-XX:-UsePerfData" not in (env.get("JAVA_OPTS") or ""):
            problems.append("java-platform JAVA_OPTS lacks -XX:-UsePerfData")
        if env.get("IAP_BIND_ADDR") != "0.0.0.0":
            problems.append("java-platform must set IAP_BIND_ADDR=0.0.0.0")
        state = (vols.get(mounts.get("/data", ""), {}).get("persistentVolumeClaim") or {}).get(
            "claimName"
        )
        if state != "iap-java-state":
            problems.append(
                f"java-platform /data claim is {state!r}, expected its own iap-java-state"
            )
    jenv = compose_doc()["services"]["java-platform"].get("environment") or {}
    if isinstance(jenv, list):
        jenv = dict(e.split("=", 1) for e in jenv if "=" in e)
    if str(jenv.get("IAP_BIND_ADDR")) != "0.0.0.0":
        problems.append("compose java-platform must set IAP_BIND_ADDR=0.0.0.0")
    record(
        "k8s_pod_hardening",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:6]) or f"{len(pods)} workloads",
    )


# PVC -> workloads allowed to mount it in addition to its owner, READ-ONLY and
# pinned to the owner's node (check_k8s_state_backup enforces both).
SANCTIONED_READERS = {"iap-java-state": ("state-backup",)}


# ---------------------------------------------------------- v1.12 P4 ops ---
AM_DIR = DEPLOY / "alertmanager"
SEVERITIES = {"page", "critical", "warning", "none"}
# Alerts that predate the "every alert has a promtool test" gate (v1.12 P4).
# This list may only SHRINK: adding a test for one of these fails the check
# until the name is removed here, and a NEW alert cannot be exempted without a
# visible diff to this file.
LEGACY_UNTESTED_ALERTS = {
    "SignalRateCollapse",
    "LiveVsBacktestDrift",
    "AlphaLifecycleRetired",
    "FillRateDrop",
    "PreTradeRejectRatioHigh",
    "GrossNotionalUtilizationHigh",
    "GcPauseHigh",
    "SessionRestartsClimbing",
}
# A committed receiver URL is only acceptable if it is local: a compose service
# name or loopback. Anything else is (or will become) a secret.
LOCAL_URL = re.compile(r"^https?://(alert-sink|localhost|127\.0\.0\.1)(:\d+)?(/\S*)?$")
TOKENISH = re.compile(
    r"(hooks\.slack\.com/services/|xox[abpr]-|routing_key:|integration_key|"
    r"api_key:|apikey|bearer\s+[A-Za-z0-9]|[?&](token|key|secret)=)",
    re.I,
)


def check_alerting_secret_free() -> None:
    problems = []
    am = yaml.safe_load((AM_DIR / "alertmanager.yml").read_text(encoding="utf-8"))
    for recv in am.get("receivers", []):
        for kind, cfgs in recv.items():
            if not kind.endswith("_configs"):
                continue
            for cfg in cfgs:
                for key in ("url", "api_url", "service_key", "routing_key", "auth_password"):
                    if key in cfg:
                        problems.append(f"receiver {recv['name']}: inline `{key}` (use *_file)")
                if kind == "webhook_configs" and "url_file" not in cfg:
                    problems.append(f"receiver {recv['name']}: webhook without url_file")
    for f in sorted(AM_DIR.iterdir()):
        text = f.read_text(encoding="utf-8")
        if TOKENISH.search(text):
            problems.append(f"{f.name}: looks like it contains a credential")
        if f.suffix == ".url" and not LOCAL_URL.match(text.strip()):
            problems.append(f"{f.name}: committed URL is not a local default")
        if "placeholder" in f.name:
            problems.append(f"{f.name}: placeholder receiver file (removed in v1.12)")
    for fname, doc in k8s_docs():
        if doc.get("kind") == "Secret" and (doc.get("data") or doc.get("stringData")):
            problems.append(f"{fname.name}: commits a Secret with data")
    record("alerting_secret_free", "PASS" if not problems else "FAIL", "; ".join(problems[:6]))


def alert_rules() -> list[dict]:
    return [r for r in all_rules() if "alert" in r]


def check_alert_rule_metadata() -> None:
    problems = []
    for rule in alert_rules():
        name = rule["alert"]
        sev = (rule.get("labels") or {}).get("severity")
        if sev not in SEVERITIES:
            problems.append(f"{name}: severity {sev!r} not in {sorted(SEVERITIES)}")
        ann = rule.get("annotations") or {}
        for key in ("runbook", "runbook_url"):
            ref = ann.get(key)
            if not ref:
                problems.append(f"{name}: no {key} annotation")
            elif not (ROOT / ref.split("#", 1)[0]).is_file():
                problems.append(f"{name}: {key} {ref} does not resolve to a file")
    record(
        "alert_rule_metadata",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:6]) or f"{len(alert_rules())} alerts",
    )


def check_alert_rule_tests() -> None:
    problems = []
    loaded: set[str] = set()
    tested: set[str] = set()
    for t in sorted((PROM / "tests").glob("*.yml")):
        doc = yaml.safe_load(t.read_text(encoding="utf-8")) or {}
        loaded |= {Path(rf).name for rf in doc.get("rule_files", [])}
        for case in doc.get("tests", []):
            for art in case.get("alert_rule_test", []) or []:
                tested.add(art.get("alertname"))
    for f in RULE_FILES:
        if f.name not in loaded:
            problems.append(f"{f.name} is not loaded by any promtool test file")
    names = {r["alert"] for r in alert_rules()}
    for name in sorted(names - tested - LEGACY_UNTESTED_ALERTS):
        problems.append(f"{name} has no promtool alert_rule_test")
    for name in sorted(LEGACY_UNTESTED_ALERTS & tested):
        problems.append(f"{name} is now tested: remove it from LEGACY_UNTESTED_ALERTS")
    for name in sorted(LEGACY_UNTESTED_ALERTS - names):
        problems.append(f"{name} no longer exists: remove it from LEGACY_UNTESTED_ALERTS")
    record(
        "alert_rule_tests",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:6])
        or f"{len(names & tested)}/{len(names)} alerts tested, "
        f"{len(LEGACY_UNTESTED_ALERTS)} legacy exemptions",
    )


def check_k8s_state_backup() -> None:
    problems = []
    job = next(
        (
            d
            for _, d in k8s_docs()
            if d["kind"] == "CronJob" and d["metadata"]["name"] == "state-backup"
        ),
        None,
    )
    if job is None:
        record("k8s_state_backup", "FAIL", "no state-backup CronJob")
        return
    spec = job["spec"]
    if spec.get("concurrencyPolicy") != "Forbid":
        problems.append("concurrencyPolicy must be Forbid")
    jspec = spec["jobTemplate"]["spec"]
    if not jspec.get("activeDeadlineSeconds"):
        problems.append("needs activeDeadlineSeconds (a Pending job must be reaped)")
    pod = jspec["template"]["spec"]
    claims = {
        (v.get("persistentVolumeClaim") or {}).get("claimName"): v for v in pod.get("volumes", [])
    }
    state = claims.get("iap-java-state")
    if state is None:
        problems.append("does not mount iap-java-state")
    else:
        if state["persistentVolumeClaim"].get("readOnly") is not True:
            problems.append("iap-java-state volume must be readOnly")
        for c in pod.get("containers", []):
            for m in c.get("volumeMounts", []):
                if m["name"] == state["name"] and m.get("readOnly") is not True:
                    problems.append("iap-java-state mount must be readOnly")
    if "iap-java-state-backup" not in claims:
        problems.append("must write to its own iap-java-state-backup claim")
    terms = ((pod.get("affinity") or {}).get("podAffinity") or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution"
    ) or []
    if not any(
        t.get("topologyKey") == "kubernetes.io/hostname"
        and (t.get("labelSelector") or {}).get("matchLabels", {}).get("app.kubernetes.io/name")
        == "java-platform"
        for t in terms
    ):
        problems.append(
            "needs REQUIRED podAffinity to java-platform on kubernetes.io/hostname "
            "(ReadWriteOnce is per node)"
        )
    record("k8s_state_backup", "PASS" if not problems else "FAIL", "; ".join(problems))


def check_k8s_network_policy() -> None:
    problems = []
    pols = [d for _, d in k8s_docs() if d["kind"] == "NetworkPolicy"]

    def deny(direction: str) -> bool:
        return any(
            (
                p["spec"].get("podSelector") == {}
                and direction in p["spec"].get("policyTypes", [])
                and not p["spec"].get(direction.lower())
            )
            for p in pols
        )

    if not deny("Ingress"):
        problems.append("no default-deny ingress policy")
    if not deny("Egress"):
        problems.append("no default-deny egress policy")
    dns = False
    for p in pols:
        for rule in p["spec"].get("egress") or []:
            ports = {(x.get("protocol"), x.get("port")) for x in rule.get("ports", [])}
            if {("UDP", 53), ("TCP", 53)} <= ports:
                dns = True
    if not dns:
        problems.append("no DNS (53 UDP+TCP) egress allowance")
    operator = False
    for p in pols:
        sel = (p["spec"].get("podSelector") or {}).get("matchLabels", {})
        if sel.get("app.kubernetes.io/name") != "java-platform":
            continue
        for rule in p["spec"].get("ingress") or []:
            froms = [f.get("podSelector", {}).get("matchLabels", {}) for f in rule.get("from", [])]
            ports = [x.get("port") for x in rule.get("ports", [])]
            if {"iap.role": "operator"} in froms and 8080 in ports:
                operator = True
    if not operator:
        problems.append(
            "no ingress rule letting iap.role=operator pods reach the java admin port 8080"
        )
    record(
        "k8s_network_policy",
        "PASS" if not problems else "FAIL",
        "; ".join(problems) or f"{len(pols)} policies",
    )


def check_compose_exposure() -> None:
    problems = []
    for name, svc in compose_doc()["services"].items():
        for port in svc.get("ports", []) or []:
            s = str(port)
            host_port = s.split(":")[-2] if s.count(":") >= 1 else s
            if host_port in ("8080", "9090", "9093") and not s.startswith("127.0.0.1:"):
                problems.append(f"{name} publishes {s} on all interfaces")
    record("compose_exposure", "PASS" if not problems else "FAIL", "; ".join(problems))


def check_image_pinning() -> None:
    problems = []
    digest = re.compile(r"@sha256:[0-9a-f]{64}$")
    for df in sorted((DEPLOY / "docker").glob("Dockerfile.*")):
        for m in re.finditer(r"^FROM\s+(\S+)", df.read_text(), re.M):
            ref = m.group(1)
            if not digest.search(ref):
                problems.append(f"{df.name}: FROM {ref} is not digest-pinned")
    iap_tags = set()

    def check_ref(where: str, ref: str) -> None:
        if ref.startswith("ghcr.io/ashjha0/intraday-alpha-platform-"):
            tag = ref.split(":", 1)[1].split("@")[0] if ":" in ref else ""
            if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
                problems.append(f"{where}: {ref} must carry a vX.Y.Z tag")
            iap_tags.add(tag)
        elif ref.startswith("iap/"):
            problems.append(f"{where}: {ref} must use the ghcr.io/ashjha0 name")
        elif not digest.search(ref):
            problems.append(f"{where}: third-party image {ref} is not digest-pinned")

    for name, svc in compose_doc()["services"].items():
        if "image" in svc:
            check_ref(f"compose {name}", svc["image"])
    for _fname, name, pod in pod_specs():
        for c in pod.get("containers", []):
            check_ref(f"k8s {name}", c["image"])
    if len(iap_tags) > 1:
        problems.append(f"iap images use several version tags: {sorted(iap_tags)}")
    record("image_pinning", "PASS" if not problems else "FAIL", "; ".join(problems[:6]))


def check_workflow_supply_chain() -> None:
    problems = []
    files = sorted(WORKFLOWS.glob("*.yml"))
    cargo = re.compile(r"cargo\s+(build|test|clippy|llvm-cov|check|run|doc)(.*)")

    def code_lines(path: Path):
        """Non-comment, non-label lines (comments/step names may say 'cargo test')."""
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", "- name:", "name:", "note ")):
                continue
            yield line.split(" #")[0]

    for f in files:
        text = f.read_text()
        doc = yaml.safe_load(text)
        if "permissions" not in doc:
            problems.append(f"{f.name}: no top-level permissions")
        for m in re.finditer(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.M):
            ref = m.group(1)
            if ref.startswith("./"):
                continue
            if not re.search(r"@[0-9a-f]{40}$", ref):
                problems.append(f"{f.name}: {ref} is not pinned to a commit SHA")
        for m in re.finditer(r"runs-on:\s*(\S+)", text):
            if m.group(1) in ("ubuntu-latest", "windows-latest", "macos-latest"):
                problems.append(f"{f.name}: unpinned runner {m.group(1)}")
        for line in code_lines(f):
            m = cargo.search(line)
            if m and "--locked" not in m.group(2):
                problems.append(f"{f.name}: cargo {m.group(1)} without --locked")
    for extra in [ROOT / "tests" / "harness" / "run_all.sh", DEPLOY / "docker" / "Dockerfile.rust"]:
        for line in code_lines(extra):
            m = cargo.search(line)
            if m and "--locked" not in m.group(2):
                problems.append(f"{extra.name}: cargo {m.group(1)} without --locked")
    record(
        "workflow_supply_chain",
        "PASS" if not problems else "FAIL",
        "; ".join(problems[:6]) or f"{len(files)} workflows",
    )


def check_rust_toolchain_pinned() -> None:
    problems = []
    toml_text = (ROOT / "rust" / "rust-toolchain.toml").read_text()
    m = re.search(r'channel\s*=\s*"([^"]+)"', toml_text)
    if not m or not re.fullmatch(r"\d+\.\d+\.\d+", m.group(1)):
        record(
            "rust_toolchain_pinned",
            "FAIL",
            "rust/rust-toolchain.toml must pin an exact X.Y.Z channel",
        )
        return
    want = m.group(1)
    for f in sorted(WORKFLOWS.glob("*.yml")):
        for tc in re.findall(r'toolchain:\s*"?([0-9][^"\s#]*)"?', f.read_text()):
            if tc != want:
                problems.append(f"{f.name}: toolchain {tc} != {want}")
        if "dtolnay/rust-toolchain" in f.read_text() and not re.search(
            r'toolchain:\s*"?\d', f.read_text()
        ):
            problems.append(f"{f.name}: rust-toolchain action without a pinned toolchain")
    df = (DEPLOY / "docker" / "Dockerfile.rust").read_text()
    if f"FROM rust:{want}-" not in df:
        problems.append(f"Dockerfile.rust build stage is not rust:{want}-*")
    cargo = (ROOT / "rust" / "Cargo.toml").read_text()
    if not re.search(r"\[profile\.release\][^\[]*overflow-checks\s*=\s*true", cargo):
        problems.append("rust/Cargo.toml [profile.release] lacks overflow-checks = true")
    record("rust_toolchain_pinned", "PASS" if not problems else "FAIL", "; ".join(problems) or want)


def check_release_guard() -> None:
    """The release workflow's guard and its dry-run trigger.

    A manual run must not be able to publish: the jobs that push images and
    write the release are conditioned on a tag push, and the guard is the
    tested script rather than an inline one-liner (the one-liner compared an
    empty API answer as a number and failed the v1.4.0 release once)."""
    problems = []
    path = WORKFLOWS / "release.yml"
    doc = yaml.safe_load(path.read_text())
    triggers = doc.get(True, doc.get("on", {})) or {}  # YAML 1.1 reads `on` as True
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") or {}
    if set(inputs) != {"dry_run", "sha"}:
        problems.append(f"workflow_dispatch inputs are {sorted(inputs)}, want dry_run and sha")
    elif inputs["dry_run"].get("default") is not True or not inputs["sha"].get("required"):
        problems.append("dry_run must default to true and sha must be required")
    if (triggers.get("push") or {}).get("tags") != ["v*"]:
        problems.append("release.yml must still run on v* tag pushes")
    jobs = doc.get("jobs", {})
    steps = jobs.get("verify-ci", {}).get("steps", [])
    if "tests/harness/verify_ci_green.py" not in " ".join(str(s.get("run", "")) for s in steps):
        problems.append("verify-ci does not run tests/harness/verify_ci_green.py")
    if not (ROOT / "tests" / "harness" / "verify_ci_green.py").is_file():
        problems.append("tests/harness/verify_ci_green.py is missing")
    for name in ("images", "manifest"):
        cond = str(jobs.get(name, {}).get("if", ""))
        if "github.event_name == 'push'" not in cond:
            problems.append(f"job {name} is not limited to tag pushes (if: {cond!r})")
    if "verify-ci" not in str(jobs.get("images", {}).get("needs", "")):
        problems.append("job images does not need verify-ci")
    if "images" not in str(jobs.get("manifest", {}).get("needs", "")):
        problems.append("job manifest does not need images")
    record("release_guard", "PASS" if not problems else "FAIL", "; ".join(problems[:6]))


def main() -> int:
    print("deployment structural validation (PLATFORM_CONVENTIONS.md §12.7 / GOVERNANCE.md §1)")
    print(f"repo: {ROOT}")
    print("prometheus:")
    check_prometheus_rules()
    check_prometheus_config()
    check_prometheus_rule_tests()
    check_metric_provenance()
    check_annotation_functions()
    print("docker:")
    check_dockerignore()
    check_dockerfile_copy_sources()
    check_docker_build_context()
    check_compose_config()
    check_compose_log_limits()
    check_config_dir_wiring()
    print("kubernetes:")
    check_k8s_parse()
    check_k8s_dry_run()
    check_k8s_singleton()
    check_configmaps_in_sync()
    check_configmap_items_in_sync()
    print("grafana / harness:")
    check_dashboards()
    check_java_golden_gate()
    print("governance hardening:")
    check_alerting_wired()
    check_alerting_secret_free()
    check_alert_rule_metadata()
    check_alert_rule_tests()
    check_k8s_state_backup()
    check_k8s_pod_hardening()
    check_k8s_network_policy()
    check_compose_exposure()
    check_image_pinning()
    check_workflow_supply_chain()
    check_rust_toolchain_pinned()
    check_release_guard()

    failed = [c for c, s, _ in RESULTS if s == "FAIL"]
    skipped = [c for c, s, _ in RESULTS if s == "SKIP"]
    passed = [c for c, s, _ in RESULTS if s == "PASS"]
    print()
    print(f"deployment checks: {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if skipped:
        print("  skipped (tool not installed): " + ", ".join(skipped))
    if failed:
        print("  FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
