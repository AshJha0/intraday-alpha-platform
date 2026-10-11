"""DeepLOB (CNN + inception + LSTM) and an MLP baseline in torch (v1.15, AI4).

Optional: needs the ``deep`` extra (torch). Importing this module without
torch raises ImportError; probe with :func:`iap.models.deeplob.torch_available`.
The dataset path lives in :mod:`iap.models.deeplob` (numpy only).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import torch
from torch import nn

from iap.models.deeplob import N_CLASSES


def _conv_block(cin: int, cout: int, kw: int, sw: int) -> nn.Module:
    return nn.Sequential(
        nn.Conv2d(cin, cout, (1, kw), stride=(1, sw)),
        nn.LeakyReLU(0.01),
        nn.BatchNorm2d(cout),
        nn.Conv2d(cout, cout, (4, 1), padding="same"),
        nn.LeakyReLU(0.01),
        nn.BatchNorm2d(cout),
        nn.Conv2d(cout, cout, (4, 1), padding="same"),
        nn.LeakyReLU(0.01),
        nn.BatchNorm2d(cout),
    )


class DeepLOBNet(nn.Module):
    """DeepLOB: input ``(N, 1, W, 4L)`` -> logits ``(N, 3)``.

    Conv 1x2 / stride 2 pairs (price, size); the second 1x2 / stride 2 pairs
    ask with bid; a 1xL conv collapses levels. Inception (1x1->3x1,
    1x1->5x1, maxpool->1x1), then an LSTM over time; the last step's hidden
    state feeds the classifier.
    """

    def __init__(self, levels: int = 10, conv: int = 16, inc: int = 32, hidden: int = 64):
        super().__init__()
        self.b1 = _conv_block(1, conv, 2, 2)
        self.b2 = _conv_block(conv, conv, 2, 2)
        self.b3 = _conv_block(conv, conv, levels, 1)

        def branch(k: int) -> nn.Module:
            return nn.Sequential(
                nn.Conv2d(conv, inc, 1),
                nn.LeakyReLU(0.01),
                nn.BatchNorm2d(inc),
                nn.Conv2d(inc, inc, (k, 1), padding="same"),
                nn.LeakyReLU(0.01),
                nn.BatchNorm2d(inc),
            )

        self.i3 = branch(3)
        self.i5 = branch(5)
        self.ip = nn.Sequential(
            nn.MaxPool2d((3, 1), stride=1, padding=(1, 0)),
            nn.Conv2d(conv, inc, 1),
            nn.LeakyReLU(0.01),
            nn.BatchNorm2d(inc),
        )
        self.lstm = nn.LSTM(3 * inc, hidden, batch_first=True)
        self.fc = nn.Linear(hidden, N_CLASSES)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.b3(self.b2(self.b1(x)))  # (N, C, W, 1)
        h = torch.cat([self.i3(h), self.i5(h), self.ip(h)], dim=1)
        h = h.squeeze(-1).permute(0, 2, 1)  # (N, W, 3*inc)
        out, _ = self.lstm(h)
        return self.fc(out[:, -1, :])


class MLPBaselineNet(nn.Module):
    """Flattened window, two hidden layers: the simpler deep baseline."""

    def __init__(self, window: int, features: int, hidden: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Flatten(),
            nn.Linear(window * features, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Linear(hidden, N_CLASSES),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class DeepLOBClassifier:
    """fit / predict_proba / predict over ``(N, W, 4L)`` windows.

    ``arch`` is ``"deeplob"`` or ``"mlp"``. Deterministic on CPU for a seed
    (``torch.manual_seed`` + a seeded numpy batch permutation).
    """

    def __init__(
        self,
        *,
        arch: str = "deeplob",
        epochs: int = 20,
        lr: float = 1e-3,
        batch_size: int = 64,
        seed: int = 0,
        **net_kwargs: Any,
    ) -> None:
        if arch not in ("deeplob", "mlp"):
            raise ValueError(f"unknown arch {arch!r}")
        self.arch = arch
        self.epochs = epochs
        self.lr = lr
        self.batch_size = batch_size
        self.seed = seed
        self.net_kwargs = net_kwargs
        self.net: nn.Module | None = None

    def _build(self, window: int, features: int) -> nn.Module:
        if self.arch == "deeplob":
            if features % 4:
                raise ValueError("deeplob needs 4*levels features")
            return DeepLOBNet(levels=features // 4, **self.net_kwargs)
        return MLPBaselineNet(window, features, **self.net_kwargs)

    def _tensor(self, x: np.ndarray) -> torch.Tensor:
        t = torch.as_tensor(x, dtype=torch.float32)
        return t.unsqueeze(1) if self.arch == "deeplob" else t

    def fit(self, x: np.ndarray, y: np.ndarray) -> DeepLOBClassifier:
        torch.manual_seed(self.seed)
        net = self._build(x.shape[1], x.shape[2])
        opt = torch.optim.Adam(net.parameters(), lr=self.lr)
        loss_fn = nn.CrossEntropyLoss()
        xt, yt = self._tensor(x), torch.as_tensor(y, dtype=torch.long)
        rng = np.random.default_rng(self.seed)
        net.train()
        for _ in range(self.epochs):
            perm = rng.permutation(len(y))
            for s in range(0, len(y), self.batch_size):
                idx = torch.as_tensor(perm[s : s + self.batch_size])
                if len(idx) < 2:  # BatchNorm needs > 1 sample
                    continue
                opt.zero_grad()
                loss_fn(net(xt[idx]), yt[idx]).backward()
                opt.step()
        self.net = net
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self.net is None:
            raise RuntimeError("fit first")
        self.net.eval()
        with torch.no_grad():
            return torch.softmax(self.net(self._tensor(x)), dim=1).numpy()

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.predict_proba(x).argmax(axis=1)
