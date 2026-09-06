cmake_minimum_required(VERSION 3.16)
project(iap_cpp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Werror)

# Eigen (header-only; FX05 currency-factor pseudo-inverse only).
find_package(Eigen3 3.3 QUIET NO_MODULE)
if(NOT TARGET Eigen3::Eigen)
  find_path(IAP_EIGEN_DIR Eigen/Dense PATHS /usr/include/eigen3 /usr/local/include/eigen3)
  if(NOT IAP_EIGEN_DIR)
    message(FATAL_ERROR "Eigen3 not found (needed by iap/alpha FX05 solve)")
  endif()
endif()

# ------------------------------------------------------------------ library
add_library(iap_core STATIC
  src/marketdata/events.cpp
  src/marketdata/codec.cpp
  src/orderbook/book.cpp
  src/replay/replay.cpp
  src/replay/exec_replay.cpp
  src/features/feature_engine.cpp
  src/alpha/alpha.cpp
  src/execution/execution.cpp
  src/execution/algos.cpp
  src/sor/sor.cpp
)
target_include_directories(iap_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
if(TARGET Eigen3::Eigen)
  target_link_libraries(iap_core PRIVATE Eigen3::Eigen)
else()
  target_include_directories(iap_core SYSTEM PRIVATE ${IAP_EIGEN_DIR})
endif()

# -------------------------------------------------------------------- tests
enable_testing()
find_package(GTest REQUIRED)
include(GoogleTest)

set(IAP_GOLDEN_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../tests/golden")

function(iap_add_test name)
  add_executable(${name} tests/${name}.cpp)
  target_link_libraries(${name} PRIVATE iap_core GTest::gtest GTest::gtest_main)
  target_compile_definitions(${name} PRIVATE IAP_GOLDEN_DIR="${IAP_GOLDEN_DIR}")
  gtest_discover_tests(${name})
endfunction()

iap_add_test(test_rng)
iap_add_test(test_events)
iap_add_test(test_codec)
iap_add_test(test_book)
iap_add_test(test_replay)
iap_add_test(test_scenarios_core)
iap_add_test(test_golden)
iap_add_test(test_features_golden)
iap_add_test(test_features_brute)
iap_add_test(test_alpha_golden)
iap_add_test(test_execution)
iap_add_test(test_exec_algos)
iap_add_test(test_replay_fills)

# -------------------------------------------------------------------- tools
# One-shot golden generator (expected_replay_fills.json; refuses overwrite).
add_executable(make_replay_fills_golden tools/make_replay_fills_golden.cpp)
target_link_libraries(make_replay_fills_golden PRIVATE iap_core)

# -------------------------------------------------------------------- bench
# bench_all supersedes bench_core: decode/book/replay plus feature engine,
# alpha scoring and execution-sim replay. Writes benchmarks/results_cpp.md
# when given an output path.
add_executable(bench_all bench/bench_all.cpp)
target_link_libraries(bench_all PRIVATE iap_core)
target_compile_definitions(bench_all PRIVATE IAP_GOLDEN_DIR="${IAP_GOLDEN_DIR}")
