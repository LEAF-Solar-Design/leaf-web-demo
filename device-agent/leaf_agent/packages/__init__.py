"""Trusted local handlers only. No entry points or remotely named imports.

Handlers must be spawn-picklable and persist state on disk (operations run in
isolated processes). All return values must be credential-free JSON. auth_headers
is local-only; never return its values in state() or handle(). P5 registers HA.
"""
from typing import Protocol


class Handler(Protocol):
    def handle(self, op: str, args: dict): ...
    def auth_headers(self) -> dict[str, str]: ...
    def state(self) -> dict | None: ...


class Registry:
    def __init__(self):
        self.handlers: dict[str, Handler] = {}

    def register(self, package: str, handler: Handler) -> None:
        if package in self.handlers:
            raise ValueError("Package already registered")
        self.handlers[package] = handler

    def get(self, package: str) -> Handler | None:
        return self.handlers.get(package)

    def states(self) -> list[dict]:
        states = []
        for handler in self.handlers.values():
            state = handler.state()
            if state is not None:
                states.append(state)
        return states[:64]
