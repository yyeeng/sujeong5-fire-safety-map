"""부산 골목 화재안전 지도 생성 패키지."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "project.yaml"

__all__ = ["DEFAULT_CONFIG", "PROJECT_ROOT"]
__version__ = "1.0.0"
