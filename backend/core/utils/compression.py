# utils/compression.py
import zlib
import base64
import logging

logger = logging.getLogger(__name__)

def compress_text(text: str) -> str:
    """Compress input text using zlib and base64 encoding"""
    try:
        compressed = zlib.compress(text.encode('utf-8'))
        return base64.b64encode(compressed).decode('utf-8')
    except Exception as e:
        logger.error(f"Compression error: {e}")
        return text

def decompress_text(compressed_text: str) -> str:
    """Decompress base64 encoded zlib compressed text"""
    try:
        decoded = base64.b64decode(compressed_text)
        return zlib.decompress(decoded).decode('utf-8')
    except Exception as e:
        logger.error(f"Decompression error: {e}")
        return compressed_text