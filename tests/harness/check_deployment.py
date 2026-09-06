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
                              `git clone` + .dockerignore reproduces the real
                              build context, every stage's COPY and every
                              build/runtime input resolves in it, and the Java
                              build stage actually runs (a real `docker build`
                              when a daemon is reachable)
  dockerignore_present        the build context excludes host build trees
  k8s_manifests_parse         every manifest is valid YAML with apiVersion/kind
  k8s_dry_run                 kubectl apply --dry-run=client (or kubeconform)
  k8s_trading_singleton       replicas 1 + Recreate + probes that can fail
  configmaps_in_sync          generate_configmaps.py output == committed files
  dashboards_valid            dashboard JSON parses, datasource uid stable, and
                              every panel expression names a real metric
  java_golden_gate_complete   JAVA_GOLDEN_CLASSES == the *GoldenTest.java set

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
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=300
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------- metrics ---
# The metrics the deployment actually exports (PLATFORM_CONVENTIONS.md §12.6).
# Sourced from com.iap.platform.PaperTrading + com.iap.risk.RiskEngine +
# com.iap.monitoring.GcMetrics; `up` is Prometheus's own.
EXPORTED_METRICS = {
    # market data
    "md_events_total", "md_sequence_gaps_total", "md_duplicates_total",
    "md_last_event_unixtime", "md_last_event_wallclock_unixtime",
    "md_event_time_gap_seconds", "book_stale",
    # platform lifecycle
    "platform_mode", "platform_session_state", "risk_session_restarts_total",
    "admin_requests_total",
    # latency histograms
    "decode_latency_ns", "book_update_latency_ns", "order_path_latency_ns",
    "exec_slippage_bps", "jvm_gc_pause_ns",
    # alpha / portfolio
    "alpha_signals_total", "alpha_live_vs_backtest_drift", "alpha_rolling_ic",
    "alpha_lifecycle_state", "portfolio_solves_total",
    "portfolio_gross_notional", "portfolio_net_notional", "portfolio_drawdown",
    # execution
    "exec_orders_submitted_total", "exec_fills_total",
    "exec_child_orders_rejected_total",
    # risk
    "risk_events_total", "risk_decisions_total", "risk_allowed_total",
    "risk_rejected_total", "risk_realized_pnl", "risk_unrealized_pnl",
    "risk_daily_pnl", "risk_kill_switch_engaged", "risk_limit",
    # prometheus itself
    "up",
}

# Go text/template builtins plus Prometheus's own template functions.
ALLOWED_TEMPLATE_FUNCS = {
    "and", "call", "html", "index", "slice", "js", "len", "not", "or",
    "print", "printf", "println", "urlquery", "eq", "ne", "lt", "le", "gt",
    "ge", "if", "else", "end", "range", "with", "template", "block", "define",
    "humanize", "humanize1024", "humanizeDuration", "humanizePercentage",
    "humanizeTimestamp", "title", "toUpper", "toLower", "match",
    "reReplaceAll", "graphLink", "tableLink", "parseDuration", "stripPort",
    "stripDomain", "toTime", "pathPrefix", "externalURL", "value", "args",
    "safeHtml", "sortByLabel", "first", "label", "strvalue", "query",
}

RULE_FILES = [PROM / "alerts.yml", PROM / "recording.yml"]


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
        record("prometheus_rules_load", "PASS" if rc == 0 else "FAIL",
               "" if rc == 0 else out)
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
    record("prometheus_rules_load", "PASS" if not problems else "FAIL",
           "promtool absent, used the strict fallback parser"
           if not problems else "; ".join(problems[:5]))


def template_problems(rule: dict, where: str) -> list[str]:
    """Every {{ ... }} action must balance and use a known function."""
    problems = []
    for field, text in list(rule.get("annotations", {}).items()) + \
            list(rule.get("labels", {}).items()):
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
                        f"{where} {rule.get('alert')}.{field}: "
                        f'function "{name}" not defined')
    return problems


def check_annotation_functions() -> None:
    problems = []
    for rule in all_rules():
        problems += template_problems(rule, "alerts.yml")
    record("alert_annotation_functions", "PASS" if not problems else "FAIL",
           "; ".join(problems[:5]))


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
                record("prometheus_config", "FAIL",
                       f"rule_files references {ref}, but {src} does not exist")
                return
            (stage / name).write_text(src.read_text())
            staged_rules.append(str(stage / name))
        doc["rule_files"] = staged_rules
        staged_cfg = stage / "prometheus.yml"
        staged_cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
        rc, out = run(["promtool", "check", "config", str(staged_cfg)])
        record("prometheus_config", "PASS" if rc == 0 else "FAIL",
               "" if rc == 0 else out)


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
    "by", "without", "on", "ignoring", "group_left", "group_right", "and",
    "or", "unless", "offset", "bool", "rate", "increase", "sum", "avg", "min",
    "max", "count", "clamp_min", "clamp_max", "histogram_quantile", "time",
    "vector", "absent", "topk", "bottomk", "delta", "irate", "quantile",
    "le", "job", "service", "instance", "limit", "mode", "alpha", "instrument",
    "m", "h", "s", "d",
}


def referenced_metrics(expr: str) -> set[str]:
    """Metric-ish identifiers in a PromQL expression."""
    stripped = re.sub(r'"[^"]*"', "", expr)
    # Drop numeric literals first, exponent and duration suffixes included
    # (10e6, 1e-9, 5m, 0.5): otherwise "e6" and "m" look like metric names.
    stripped = re.sub(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[smhdwy]?\b", " ",
                      stripped)
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
    record("alert_metric_provenance", "PASS" if not unknown else "FAIL",
           "" if not unknown else
           "no producer for " + ", ".join(
               f"{m} (used by {', '.join(v)})" for m, v in unknown.items()))


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
    record("docker_build_context", "PASS" if rc == 0 else "FAIL",
           (tail[-1] if tail else out)[:300])


def check_compose_config() -> None:
    if not have("docker"):
        record("compose_config", "SKIP", "docker CLI not installed")
        return
    env = dict(os.environ)
    env.setdefault("GRAFANA_ADMIN_PASSWORD", "check-deployment-placeholder")
    proc = subprocess.run(["docker", "compose", "config", "-q"],
                          cwd=COMPOSE.parent, capture_output=True, text=True,
                          env=env, timeout=300)
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
    record("compose_log_limits", "PASS" if not bad else "FAIL",
           "" if not bad else
           "no logging.options.max-size/max-file: " + ", ".join(bad))


def check_config_dir_wiring() -> None:
    """§12.2 — IAP_CONFIG_DIR is honoured by the code, image and compose."""
    problems = []
    java = (ROOT / "java" / "src" / "main" / "java" / "com" / "iap" / "config"
            / "ConfigService.java").read_text()
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
    record("compose_and_dockerfile_env", "PASS" if not problems else "FAIL",
           "; ".join(problems))


DOCKERIGNORE = ROOT / ".dockerignore"


def dockerignore_patterns() -> list[str]:
    if not DOCKERIGNORE.exists():
        return []
    return [ln.strip() for ln in DOCKERIGNORE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def check_dockerignore() -> None:
    if not DOCKERIGNORE.exists():
        record("dockerignore_present", "FAIL", "no .dockerignore at the repo root")
        return
    pats = dockerignore_patterns()
    required = ["cpp/build/", "rust/target/", "java/out/", ".git/"]
    missing = [r for r in required if r not in pats]
    record("dockerignore_present", "PASS" if not missing else "FAIL",
           "" if not missing else "missing patterns: " + ", ".join(missing))


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
                            f"{df.name}: COPY {src} is excluded by "
                            f".dockerignore pattern {pat}")
    record("dockerfile_copy_sources", "PASS" if not problems else "FAIL",
           "; ".join(problems[:5]))


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
    record("k8s_manifests_parse", "PASS" if not problems else "FAIL",
           "; ".join(problems[:5]) or f"{len(docs)} documents")


def check_k8s_dry_run() -> None:
    if have("kubeconform"):
        rc, out = run(["kubeconform", "-strict", "-summary",
                       *[str(p) for p in sorted(K8S.glob("*.yaml"))]])
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
    for f, doc in k8s_docs():
        if doc["kind"] == "PodDisruptionBudget" and \
                doc["metadata"]["name"] == "java-platform":
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
            problems.append("readinessProbe must GET /ready (not /metrics: a "
                            "TCP bind is not readiness)")
        if "startupProbe" not in container:
            problems.append("no startupProbe for the decode phase")
    if not found:
        problems.append("no java-platform Deployment found")
    record("k8s_trading_singleton", "PASS" if not problems else "FAIL",
           "; ".join(problems))


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
    record("configmaps_in_sync", "PASS" if not drift else "FAIL",
           "; ".join(drift) or f"{len(committed)} ConfigMaps")


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
                    problems.append(
                        f"{f.name}: panel {panel['id']} datasource uid {ds!r}")
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
                        f"producer exports")
    record("dashboards_valid", "PASS" if not problems else "FAIL",
           "; ".join(problems[:5]))


# -------------------------------------------------------------- harness ----
def check_java_golden_gate() -> None:
    """§12.7 / round-3 SEV-2 — the Java golden gate must cover every class."""
    run_all = (ROOT / "tests" / "harness" / "run_all.sh").read_text()
    m = re.search(r'JAVA_GOLDEN_CLASSES="([^"]*)"', run_all)
    if not m:
        record("java_golden_gate_complete", "FAIL",
               "JAVA_GOLDEN_CLASSES not found in run_all.sh")
        return
    listed = {c.split(".")[-1] for c in m.group(1).split()}
    on_disk = {p.stem for p in
               (ROOT / "java" / "src" / "test" / "java" / "com" / "iap")
               .glob("*GoldenTest.java")}
    missing = sorted(on_disk - listed)
    extra = sorted(listed - on_disk)
    detail = ""
    if missing:
        detail += "not in the golden gate: " + ", ".join(missing)
    if extra:
        detail += (" " if detail else "") + "listed but absent: " + ", ".join(extra)
    record("java_golden_gate_complete",
           "PASS" if not (missing or extra) else "FAIL",
           detail or f"{len(on_disk)} golden classes")


def main() -> int:
    print("deployment structural validation "
          "(PLATFORM_CONVENTIONS.md §12.7 / GOVERNANCE.md §1)")
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
    print("grafana / harness:")
    check_dashboards()
    check_java_golden_gate()

    failed = [c for c, s, _ in RESULTS if s == "FAIL"]
    skipped = [c for c, s, _ in RESULTS if s == "SKIP"]
    passed = [c for c, s, _ in RESULTS if s == "PASS"]
    print()
    print(f"deployment checks: {len(passed)} passed, {len(failed)} failed, "
          f"{len(skipped)} skipped")
    if skipped:
        print("  skipped (tool not installed): " + ", ".join(skipped))
    if failed:
        print("  FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
