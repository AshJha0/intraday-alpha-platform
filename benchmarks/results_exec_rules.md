# C++ execution-rule latency (bench_exec_rules, v1.17 F7)

No table has been transcribed into this file: the `bench-exec-rules` artifact is produced by
the C++ benchmark job on every run (weekly `cpp-full` since v2.0 stage 11, `ci.yml` before that),
and `.github/workflows/ci.yml` says the figures are copied by hand, which has not happened
since the step was added in v1.17 (recorded in archive/docs/DOC_AUDIT_v2.md, 2026-10-25). Until a run
id, runner CPU and table are pasted here, no execution-rule latency figure is quotable anywhere
in the documentation. Methodology: `cpp/bench/bench_exec_rules.cpp` and benchmarks/RESULTS.md.
No latency edge is claimed; benchmarks are CI-runner numbers, not colocation.
