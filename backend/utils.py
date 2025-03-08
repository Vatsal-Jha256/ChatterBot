# utils.py
from functools import lru_cache

@lru_cache(maxsize=100)
def cached_compress_input(tokenizer, prompt: str, max_length: int = 512) -> str:
    """Compress input by truncating tokens if needed."""
    tokens = tokenizer.encode(prompt)
    if len(tokens) > max_length:
        tokens = tokens[:max_length]
        return tokenizer.decode(tokens)
    return prompt