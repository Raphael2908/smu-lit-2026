"""Structured JSON logging with run correlation."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
layer_var: ContextVar[str | None] = ContextVar("layer", default=None)


def _add_context(_logger, _name, event_dict):
    if run_id := run_id_var.get():
        event_dict["run_id"] = run_id
    if layer := layer_var.get():
        event_dict["layer"] = layer
    return event_dict


def configure_logging(level: str = "info") -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "verifier"):
    return structlog.get_logger(name)
