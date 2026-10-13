"""配置应用统一使用的结构化文本日志。"""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> logging.Logger:
    """初始化根日志格式并返回应用命名日志器。"""

    logger = logging.getLogger("learning_agent")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger
