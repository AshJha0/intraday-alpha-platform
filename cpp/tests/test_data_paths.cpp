// iap/util/data_paths.hpp — lexical path normalisation and the runtime
// resolution of the data roots.
//
// Regression origin (CI images job, 2026-09-20): a compiled-in build-tree
// path ".../cpp/../tests/golden" was used at runtime inside an image that
// carries tests/golden but not cpp/, so every open failed on container
// start. The two properties that make that impossible are pinned here:
// derived paths are collapsed lexically (never needing an intermediate
// directory to be walkable), and the root is overridable by environment.

#include <gtest/gtest.h>

#include <cstdlib>
#include <string>

#include "iap/util/data_paths.hpp"

using iap::configs_dir;
using iap::data_dir;
using iap::golden_dir;
using iap::golden_file;
using iap::normalize_path;
using iap::repo_root;

namespace {

// Restores $IAP_GOLDEN_DIR on scope exit so cases cannot leak into each
// other (or into the rest of the binary's tests) whatever they assert.
class ScopedGoldenDir {
public:
    explicit ScopedGoldenDir(const char* value) {
        const char* previous = std::getenv("IAP_GOLDEN_DIR");
        had_previous_ = previous != nullptr;
        if (had_previous_) previous_ = previous;
        if (value == nullptr) {
            ::unsetenv("IAP_GOLDEN_DIR");
        } else {
            ::setenv("IAP_GOLDEN_DIR", value, 1);
        }
    }
    ~ScopedGoldenDir() {
        if (had_previous_) {
            ::setenv("IAP_GOLDEN_DIR", previous_.c_str(), 1);
        } else {
            ::unsetenv("IAP_GOLDEN_DIR");
        }
    }
    ScopedGoldenDir(const ScopedGoldenDir&) = delete;
    ScopedGoldenDir& operator=(const ScopedGoldenDir&) = delete;

private:
    bool had_previous_;
    std::string previous_;
};

}  // namespace

TEST(DataPaths, NormalizeCollapsesDotDotLexically) {
    // The exact shape that broke the container: the "cpp" hop disappears, so
    // the result is walkable wherever /build/tests/golden exists, whether or
    // not /build/cpp does.
    EXPECT_EQ(normalize_path("/build/cpp/../tests/golden"), "/build/tests/golden");
    EXPECT_EQ(normalize_path("/build/tests/golden/../../configs"), "/build/configs");
    EXPECT_EQ(normalize_path("/a/b/c/../../d"), "/a/d");
}

TEST(DataPaths, NormalizeHandlesDotsSlashesAndRoots) {
    EXPECT_EQ(normalize_path("/a/./b"), "/a/b");
    EXPECT_EQ(normalize_path("/a//b///c"), "/a/b/c");
    EXPECT_EQ(normalize_path("/a/b/"), "/a/b");
    EXPECT_EQ(normalize_path("/"), "/");
    EXPECT_EQ(normalize_path("/.."), "/");        // cannot escape the root
    EXPECT_EQ(normalize_path("/../.."), "/");
    EXPECT_EQ(normalize_path("/a/../.."), "/");
    EXPECT_EQ(normalize_path(""), "");            // empty in, empty out
}

TEST(DataPaths, NormalizeKeepsRelativeEscapes) {
    // A relative path may legitimately climb above its own start; folding
    // those away would silently change which directory is meant.
    EXPECT_EQ(normalize_path("../tests/golden"), "../tests/golden");
    EXPECT_EQ(normalize_path("a/../../b"), "../b");
    EXPECT_EQ(normalize_path("../../a"), "../../a");
    EXPECT_EQ(normalize_path("./a/b"), "a/b");
    EXPECT_EQ(normalize_path("a/.."), ".");
}

TEST(DataPaths, NormalizeIsIdempotent) {
    for (const char* path : {"/build/cpp/../tests/golden", "/a//b/./c/../d",
                             "../x/../y", "/", "a/.."}) {
        const std::string once = normalize_path(path);
        EXPECT_EQ(normalize_path(once), once) << path;
    }
}

TEST(DataPaths, EnvironmentOverridesTheCompiledInRoot) {
    const ScopedGoldenDir override_dir("/opt/iap/tests/golden");
    EXPECT_EQ(golden_dir(), "/opt/iap/tests/golden");
    EXPECT_EQ(repo_root(), "/opt/iap");
    EXPECT_EQ(configs_dir(), "/opt/iap/configs");
    EXPECT_EQ(data_dir(), "/opt/iap/data");
    EXPECT_EQ(golden_file("events_eq_mbo.jsonl"),
              "/opt/iap/tests/golden/events_eq_mbo.jsonl");
}

TEST(DataPaths, OverrideIsNormalizedToo) {
    const ScopedGoldenDir override_dir("/build/cpp/../tests/golden");
    EXPECT_EQ(golden_dir(), "/build/tests/golden");
    EXPECT_EQ(configs_dir(), "/build/configs");
}

TEST(DataPaths, EmptyOverrideFallsBackToTheCompiledInRoot) {
    // An unset variable and one set to "" must behave the same: a container
    // that declares ENV IAP_GOLDEN_DIR= must not silently resolve to "/".
    const std::string compiled_in = normalize_path(IAP_GOLDEN_DIR);
    {
        const ScopedGoldenDir override_dir("");
        EXPECT_EQ(golden_dir(), compiled_in);
    }
    {
        const ScopedGoldenDir override_dir(nullptr);
        EXPECT_EQ(golden_dir(), compiled_in);
    }
}

TEST(DataPaths, ResolvedRootsContainNoDotDotAndAreAbsolute) {
    // What the container actually depends on, asserted against whatever the
    // build baked in rather than a hard-coded path.
    const ScopedGoldenDir override_dir(nullptr);
    for (const std::string& dir : {golden_dir(), repo_root(), configs_dir(),
                                   data_dir()}) {
        ASSERT_FALSE(dir.empty());
        EXPECT_EQ(dir.front(), '/') << dir;
        EXPECT_EQ(dir.find(".."), std::string::npos) << dir;
    }
}
