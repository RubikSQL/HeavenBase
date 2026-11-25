"""Child process for the dropping-reset tests: each run is a new process over the HOME it is given."""

import os
import sys

import heavenbase as hb
from heavenbase.context import default_context
from heavenbase.llm.cache import LLMCache
from heavenbase.utils.config import ConfigManager

KEY = {"object_id": "text-row", "spec_key": "spec", "request_key": "request", "args": {}}


def _outcome(reset):
    try:
        reset()
    except Exception as exc:
        return f"raised {type(exc).__name__} {str(exc)[:80]}"
    return "returned"


def main(mode):
    if mode == "write":
        LLMCache.current().set_text(KEY, [], {"text": "default answer"}, [], policy="deterministic")
        return "written"
    if mode == "drop-cm":
        LLMCache.reset(drop=True, config_manager=hb.CM_HVNB)
        return f"dropped {LLMCache.current().get_text(KEY)}"
    if mode == "drop-pw":
        LLMCache.reset(drop=True)
        from heavenbase.context.runtime import _default_context_instance

        built = _default_context_instance() is not None
        found = LLMCache.current().get_text(KEY)
        return f"default-built={built} {'kept' if found is not None else 'dropped'}"
    if mode == "explicit-cm":
        from heavenbase._initialization import configure_explicit_context

        configure_explicit_context()
        return _outcome(lambda: LLMCache.reset(drop=True, config_manager=hb.CM_HVNB))
    if mode == "closed-default-pw":
        default_context().close()
        return _outcome(lambda: LLMCache.reset(drop=True))
    if mode == "closed-default-cm":
        default_context().close()
        return _outcome(lambda: LLMCache.reset(drop=True, config_manager=hb.CM_HVNB))
    if mode == "closed-default-other":
        root = os.path.join(os.environ["HOME"], "other-root")
        other = hb.Context.load(
            {
                "version": 1,
                "root": root,
                "workspace": "default",
                "backend": {"type": "inmem", "module": "heavenbase.backends.inmem.backend", "name": "system"},
                "registry": {"hash": "sha256", "max_items": None, "max_bytes": 1024 * 1024},
            },
            config=ConfigManager(root=root, setup=False),
        )
        LLMCache.current(config_manager=other.config).set_text(KEY, [], {"text": "other answer"}, [], policy="deterministic")
        default_context().close()
        outcome = _outcome(lambda: LLMCache.reset(drop=True)).split(" ")[0]
        found = LLMCache.current(config_manager=other.config).get_text(KEY)
        return f"{outcome} other-rows {'kept' if found is not None else 'dropped'}"
    raise ValueError(mode)


if __name__ == "__main__":
    print(main(sys.argv[1]))
