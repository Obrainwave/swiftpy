from __future__ import annotations

import hashlib
import math


def generate_deterministic_embedding(text: str, dimensions: int = 1536) -> list[float]:
    """
    Generate deterministic, normalized test embeddings using character n-grams and SHA-256 feature hashing.

    This provides dependency-free embeddings for development and functional testing;
    it is not a substitute for trained semantic embedding models.
    """
    if not text or not text.strip():
        return [0.0] * dimensions

    vec = [0.0] * dimensions
    raw_words = [w.strip(".,;:!?\"'()[]{}").lower() for w in text.split() if w.strip()]

    for word in raw_words:
        tokens = [f"<{word}>"]
        word_len = len(word)
        for n in (3, 4, 5):
            if word_len >= n:
                for i in range(word_len - n + 1):
                    tokens.append(word[i : i + n])

        for tok in tokens:
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            for i in range(2):
                idx = int.from_bytes(digest[i * 4 : (i + 1) * 4], "big") % dimensions
                sign = 1.0 if digest[i] % 2 == 0 else -1.0
                vec[idx] += sign

    # Normalize vector to unit length (L2 norm)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        vec = [x / norm for x in vec]

    return vec
