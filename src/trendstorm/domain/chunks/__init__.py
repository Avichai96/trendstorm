"""Chunks domain package."""
from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.chunks.repository import ChunkRepository

__all__ = ["Chunk", "ChunkRepository"]
