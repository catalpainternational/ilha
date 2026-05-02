"""Shared multi-strategy HTTP probe primitives.

Two consumer shapes need the same set of strategies (curl → wget → python →
``/dev/tcp``):

* ``deploy_utils.DockerCommand.health_check_http`` runs each strategy as an
  independent ``docker exec`` via ``subprocess.run`` and falls through to the
  next on non-zero exit.
* ``SlimDeployManager._build_smoke_probe_command`` and
  ``DeployDoctor._build_smoke_probe_command`` build a single ``sh -c``
  chain (``if curl-exists; then curl; elif wget-exists; then wget; ...``)
  and execute it via ``docker compose exec``.

Each consumer composes the same primitives — the strategy bodies live here
once. Adding a new strategy or fixing a probe semantics applies everywhere.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable, List, Tuple


# Strategy names, in canonical preference order. ``curl``/``wget`` are
# strongest signals (real HTTP request, fail on >=400). ``python`` works in
# slim images that ship neither and accepts 2xx/3xx (a Django app behind
# ``SECURE_PROXY_SSL_HEADER`` returns 301 to HTTPS on in-container HTTP,
# which still proves "alive"). ``tcp`` is the loosest fallback — confirms
# something speaks HTTP on the port without grading the response.
DEFAULT_STRATEGIES: Tuple[str, ...] = ("curl", "wget", "python", "tcp")


def normalize_path(path: str) -> str:
    """Normalize a smoke/health path: ensure leading ``/``, default to ``/``.

    Empty / None / whitespace-only inputs all collapse to ``/``. A leading
    slash is added if missing so downstream URL building (``base + path``)
    produces a well-formed URL regardless of how the path was declared in
    config or detected from a label.
    """
    p = (path or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p or "/"


@dataclass(frozen=True)
class ProbeStep:
    """One strategy: a guard that detects whether the strategy can run, plus
    the command body that performs the probe.

    Both fields are POSIX-shell snippets safe to drop into ``sh -c`` or chain
    with ``&&``/``elif``.
    """

    name: str
    exists_check: str
    probe_command: str

    def standalone(self) -> str:
        """``exists && probe`` — for subprocess invocation as a single ``sh -c``.

        Returns non-zero when the strategy isn't available *or* the probe
        itself failed; the caller can't distinguish the two but doesn't need
        to (just try the next strategy).
        """
        return f"{self.exists_check} && {self.probe_command}"


def _python_probe_inner(port: int, path: str, timeout: int) -> str:
    """Inline Python script: connects via ``http.client`` (no auto-redirect),
    accepts 2xx/3xx as healthy. Returned unquoted; caller wraps with
    ``shlex.quote`` when embedding."""
    return (
        "import sys, http.client\n"
        f"c = http.client.HTTPConnection('127.0.0.1', {port}, timeout={timeout})\n"
        f"c.request('GET', {path!r})\n"
        "status = c.getresponse().status\n"
        "sys.exit(0 if 200 <= status < 400 else 1)\n"
    )


def build_probe_steps(
    *,
    port: int,
    path: str,
    timeout: int = 5,
    strategies: Iterable[str] = DEFAULT_STRATEGIES,
) -> List[ProbeStep]:
    """Materialise ``ProbeStep`` instances for the requested strategies, in
    the given order. The URL probed is always
    ``http://127.0.0.1:{port}{path}`` — these probes run *inside* the target
    container so loopback is correct.
    """
    url = f"http://127.0.0.1:{port}{path}"
    steps: List[ProbeStep] = []

    for strategy in strategies:
        if strategy == "curl":
            steps.append(ProbeStep(
                name="curl",
                exists_check="command -v curl >/dev/null 2>&1",
                probe_command=f"curl -fsS --max-time {timeout} {shlex.quote(url)}",
            ))
        elif strategy == "wget":
            steps.append(ProbeStep(
                name="wget",
                exists_check="command -v wget >/dev/null 2>&1",
                probe_command=f"wget -q -T {timeout} -O - {shlex.quote(url)} >/dev/null",
            ))
        elif strategy == "python":
            inner = _python_probe_inner(port=port, path=path, timeout=timeout)
            steps.append(ProbeStep(
                name="python",
                # Use whichever python the image ships; some have only python3,
                # some symlink ``python`` only. Existence check exports ``$PY``
                # so the probe body can reference it without re-discovering.
                exists_check='PY="$(command -v python3 || command -v python)" && [ -n "$PY" ]',
                probe_command=f'"$PY" -c {shlex.quote(inner)}',
            ))
        elif strategy == "tcp":
            steps.append(ProbeStep(
                name="tcp",
                # ``/dev/tcp`` is a bash builtin; dash will fail at probe time
                # rather than at exists-check time. We still report it as the
                # last-resort fallback because the failure is informative.
                exists_check=":",
                probe_command=(
                    f"exec 3<>/dev/tcp/127.0.0.1/{port} && "
                    f"printf 'GET {path} HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3 && "
                    f"head -c 12 <&3 | grep -q '^HTTP/'"
                ),
            ))
        else:
            raise ValueError(f"Unknown probe strategy: {strategy!r}")

    return steps


def build_probe_shell_chain(
    *,
    port: int,
    path: str,
    timeout: int = 5,
    strategies: Iterable[str] = DEFAULT_STRATEGIES,
    tail: str = "",
) -> str:
    """Return a single ``sh -c``-able snippet that tries each strategy in
    order via ``if/elif/else``. ``tail`` is appended to each probe body
    (typically ``" >/dev/null"`` for quiet probing or ``" 2>&1"`` to
    surface output).

    The result is suitable for embedding inside another shell command, for
    example ``docker compose exec -T web sh -c "<chain>"``.
    """
    steps = build_probe_steps(
        port=port, path=path, timeout=timeout, strategies=strategies
    )
    if not steps:
        raise ValueError("At least one probe strategy is required")

    parts: List[str] = []
    for i, step in enumerate(steps):
        keyword = "if" if i == 0 else "elif"
        parts.append(f"{keyword} {step.exists_check}; then {step.probe_command}{tail};")
    parts.append("fi")
    return " ".join(parts)
