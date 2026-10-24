"""Unit checks (run by hand): P0 == known_null.cluster_inference; CR1 == CR2 == CR3 SE; P1 == p_student_t;
complete-null Holm rejection == min p <= alpha / K."""

import numpy as np
from generators import generate
from procedures import cr1, cr2_se, cr3_se, kn, p_p0, p_p1

for g in (5, 7, 10):
    x = generate("b1", g, 20).transpose(1, 0, 2).reshape(g, -1)[:, :48]
    p0 = p_p0(x)
    ref = np.array([kn.cluster_inference(x[:, j], 0.025)["p"] for j in range(x.shape[1])])
    assert np.max(np.abs(p0 - ref)) < 1e-12, (g, np.max(np.abs(p0 - ref)))
    _, se, _ = cr1(x)
    assert np.allclose(se, cr2_se(x)) and np.allclose(se, cr3_se(x)), g
    ref1 = np.array(
        [kn.cluster_inference(x[:, j], 0.025)["p_student_t"] for j in range(x.shape[1])]
    )
    assert np.allclose(p_p1(x), ref1)
    rng = np.random.default_rng(g)
    for _ in range(2000):
        for k in (3, 6, 16):
            p = rng.random(k) ** 3
            adj = kn.holm(list(p))
            assert (min(adj) <= 0.025) == (p.min() <= 0.025 / k)
print("OK")
