// Test-suite alias of the production SHA-256 (iap/util/sha256.hpp). The
// implementation moved into the library when the canonical-JSON / trace
// contract needed it; the golden tests keep their iap_test::Sha256 spelling.

#pragma once

#include "iap/util/sha256.hpp"

namespace iap_test {

using iap::Sha256;

}  // namespace iap_test
