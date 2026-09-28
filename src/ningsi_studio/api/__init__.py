"""接口层：入参校验、端点装配与 OpenAPI 描述。"""

from ningsi_studio.api import schemas  # noqa: F401
from ningsi_studio.api.routes import build_router  # noqa: F401

__all__ = ["schemas", "build_router"]
