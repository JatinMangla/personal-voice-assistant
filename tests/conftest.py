import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class FakeEmbedder:
    """Bag-of-words vectors: texts sharing words get high similarity. No model needed."""

    DIM = 256

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.DIM, dtype=np.float32)
        for word in text.lower().replace(".", " ").replace(",", " ").split():
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    embed_query = _vec
    embed_passage = _vec


@pytest.fixture
def embedder():
    return FakeEmbedder()
