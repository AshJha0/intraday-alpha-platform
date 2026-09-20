// Runtime resolution of the repository data roots for binaries that read
// committed artefacts (golden vectors, configs, reference data).
//
// Why this exists (CI images job, 2026-09-20). `bench_all` and the golden
// tests locate their inputs from IAP_GOLDEN_DIR, a path baked in at compile
// time by cpp/CMakeLists.txt. That path names the *build* tree, and it was
// then used at *runtime* inside a container whose layout is not the build
// tree: deployment/docker/Dockerfile.cpp copies the single bench_all binary
// plus tests/golden, configs and data/reference, and never recreates the
// cpp/ source directory. A compiled-in ".../cpp/../tests/golden" therefore
// named a path that could not be walked — the directory it resolves to was
// present, but the "cpp" hop used to get there was not — and every open
// failed on container start.
//
// Two rules follow, both enforced here rather than left to each call site:
//
//   1. Paths derived from a root are collapsed LEXICALLY (normalize_path),
//      so a derived path never depends on an intermediate directory
//      existing. "$root/../../configs" must not need "$root" itself to be
//      walkable, only the directory it denotes.
//   2. The root is overridable at runtime by $IAP_GOLDEN_DIR, so an image,
//      an installed tree or a relocated checkout states where its data is
//      instead of inheriting the machine that compiled it. Dockerfile.cpp
//      sets it; check_docker_build.py asserts that it does.

#pragma once

#include <cstdlib>
#include <string>
#include <vector>

#ifndef IAP_GOLDEN_DIR
#error "IAP_GOLDEN_DIR must be defined for this translation unit \
(cpp/CMakeLists.txt defines it for the test and bench targets only)."
#endif

namespace iap {

/// Collapse "." and ".." segments and repeated separators without touching
/// the filesystem, so the result is walkable whenever the directory it
/// denotes exists — even if the segments written to get there do not.
///
/// Lexical, therefore symlink-unaware by construction: "a/symlink/.." is
/// folded to "a", which is what a build-time path should mean and is NOT
/// what the kernel would do if `symlink` pointed elsewhere. Every path this
/// platform resolves this way is a plain committed directory, and the
/// alternative (realpath at startup) would reintroduce the very dependency
/// on intermediate directories existing that this function removes.
inline std::string normalize_path(const std::string& path) {
    if (path.empty()) return path;
    const bool absolute = path.front() == '/';
    std::vector<std::string> out;
    std::string segment;
    // A trailing empty segment is dropped with the rest; the caller's
    // trailing slash is not preserved (no call site depends on it).
    for (std::size_t i = 0; i <= path.size(); ++i) {
        if (i < path.size() && path[i] != '/') {
            segment.push_back(path[i]);
            continue;
        }
        if (segment.empty() || segment == ".") {
            segment.clear();
            continue;
        }
        if (segment == "..") {
            if (!out.empty() && out.back() != "..") {
                out.pop_back();          // fold against the previous segment
            } else if (!absolute) {
                out.push_back("..");     // relative path may escape upwards
            }                            // absolute: ".." at "/" is "/"
            segment.clear();
            continue;
        }
        out.push_back(segment);
        segment.clear();
    }
    std::string result(absolute ? "/" : "");
    for (std::size_t i = 0; i < out.size(); ++i) {
        if (i != 0) result.push_back('/');
        result += out[i];
    }
    if (result.empty()) result = absolute ? "/" : ".";
    return result;
}

/// Directory holding the committed golden vectors (tests/golden).
/// $IAP_GOLDEN_DIR wins when set and non-empty; otherwise the path this
/// translation unit was compiled against.
inline std::string golden_dir() {
    const char* override_dir = std::getenv("IAP_GOLDEN_DIR");
    if (override_dir != nullptr && override_dir[0] != '\0') {
        return normalize_path(override_dir);
    }
    return normalize_path(IAP_GOLDEN_DIR);
}

/// Repository root implied by the golden directory (its grandparent).
inline std::string repo_root() { return normalize_path(golden_dir() + "/../.."); }

/// `configs/` and `data/` under that root.
inline std::string configs_dir() { return normalize_path(repo_root() + "/configs"); }
inline std::string data_dir() { return normalize_path(repo_root() + "/data"); }

/// A named file inside the golden directory.
inline std::string golden_file(const std::string& name) {
    return normalize_path(golden_dir() + "/" + name);
}

}  // namespace iap
