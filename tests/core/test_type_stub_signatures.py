"""Every function a package type stub declares matches its runtime signature."""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

import heavenbase

pytestmark = pytest.mark.fast

PACKAGE_ROOT = Path(heavenbase.__file__).parent


def _stub_parameters(node):
    arguments = node.args
    positional = arguments.posonlyargs + arguments.args
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    kinds = ["POSITIONAL_ONLY"] * len(arguments.posonlyargs) + ["POSITIONAL_OR_KEYWORD"] * len(arguments.args)
    parameters = [(arg.arg, kind, default is not None) for arg, kind, default in zip(positional, kinds, defaults)]
    if arguments.vararg is not None:
        parameters.append((arguments.vararg.arg, "VAR_POSITIONAL", False))
    parameters += [(arg.arg, "KEYWORD_ONLY", default is not None) for arg, default in zip(arguments.kwonlyargs, arguments.kw_defaults)]
    if arguments.kwarg is not None:
        parameters.append((arguments.kwarg.arg, "VAR_KEYWORD", False))
    return parameters


def _runtime_parameters(function):
    parameters = inspect.signature(function).parameters.values()
    return [(parameter.name, parameter.kind.name, parameter.default is not inspect.Parameter.empty) for parameter in parameters]


def _stub_functions():
    for stub in sorted(PACKAGE_ROOT.rglob("*.pyi")):
        parts = list(stub.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        for node in ast.parse(stub.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                yield ".".join(parts), node


def test_stub_functions_declare_the_runtime_parameters():
    declared = list(_stub_functions())
    assert any(module == "heavenbase.dashboard" and node.name == "serve" for module, node in declared)

    for module_name, node in declared:
        function = getattr(importlib.import_module(module_name), node.name)

        assert _stub_parameters(node) == _runtime_parameters(function), f"{module_name}.{node.name}"
