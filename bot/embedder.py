"""Embeddings for the bot (a copy of parser/embedder.py: the bot l...."""
from pathlib import Path

import numpy as np

MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

try:
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    raise SystemExit("sentence-transformers is not installed for the embedder: pip install sentence-transformers") from e


class Embedder:
    def __init__(self, model_dir: str, max_len: int = 128, threads: int | None = None,
                 device: str | None = None):
        import torch

        if threads:
            torch.set_num_threads(threads)

        local_dir = Path(model_dir)
        if local_dir.is_dir():
            self.model = SentenceTransformer(str(local_dir), device=device)   # local copy
        else:
            self.model = SentenceTransformer(MODEL_ID, device=device)         # fetch/cache from HF Hub
        self.model.max_seq_length = max_len
        self.model.eval()
        self.model_id = MODEL_ID
        get_dim = getattr(self.model, "get_embedding_dimension", None)
        self.dim = get_dim() if get_dim else self.model.get_sentence_embedding_dimension()
        self.device = str(self.model.device)

    def query(self, text: str) -> np.ndarray:
        return self.model.encode(text, convert_to_numpy=True, normalize_embeddings=True)

