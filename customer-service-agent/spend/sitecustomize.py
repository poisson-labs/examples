"""Usage meter for the example's paid runs. Loaded only when PYTHONPATH points here.

Writes one JSON line per model call that LiteLLM makes (model, token usage, ok or error)
to the file named by CS_METER_LEDGER. Prices are applied afterwards, from the ledger.
It attaches when LiteLLM is first imported, so a process that never calls a model never
loads it (Prove forks one child per rollout, and the parent imports no model client).
Not part of the reader's repository; it only counts what the runs spent.
"""

import os

_LEDGER = os.environ.get("CS_METER_LEDGER")


def _attach(litellm):
    import json
    import time

    from litellm.integrations.custom_logger import CustomLogger

    def _usage(resp):
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        return {
            "input": getattr(u, "prompt_tokens", 0) or 0,
            "output": getattr(u, "completion_tokens", 0) or 0,
            "cache_read": cached or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }

    def _write(row):
        row["ts"] = time.time()
        row["pid"] = os.getpid()
        with open(_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    class _Meter(CustomLogger):
        def log_success_event(self, kwargs, response_obj, start_time, end_time):
            _write({"ok": True, "model": kwargs.get("model"), **_usage(response_obj)})

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            self.log_success_event(kwargs, response_obj, start_time, end_time)

        def log_failure_event(self, kwargs, response_obj, start_time, end_time):
            exc = kwargs.get("exception")
            _write({"ok": False, "model": kwargs.get("model"), "error": type(exc).__name__ if exc else None})

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
            self.log_failure_event(kwargs, response_obj, start_time, end_time)

    litellm.callbacks = [*getattr(litellm, "callbacks", []), _Meter()]


if _LEDGER:
    import importlib.abc
    import importlib.util
    import sys

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    class _Hook(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "litellm":
                return None
            sys.meta_path.remove(self)
            spec = importlib.util.find_spec(name)
            if spec is None or spec.loader is None:
                return spec
            run = spec.loader.exec_module

            def exec_module(module):
                run(module)
                _attach(module)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _Hook())
