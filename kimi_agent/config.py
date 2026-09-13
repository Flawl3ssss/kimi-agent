"""Central configuration for the Coomi-on-Kimi-Code agent."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "coomi-kimi"
VERSION = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
WEB_DIR = PROJECT_ROOT / "web"
DATA_DIR = Path(os.environ.get("COOMI_KIMI_HOME", Path.home() / ".coomi-kimi"))
WORKSPACE = Path(os.environ.get("COOMI_KIMI_WORKSPACE", "/workspace")).resolve()


def _first_existing(candidates: list[str]) -> str:
    for cand in candidates:
        path = Path(os.path.expanduser(cand))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def _looks_like_legacy_shim(path: str) -> bool:
    """True for the deprecated Python ``kimi-cli`` launcher, not Kimi Code."""
    try:
        with open(path, "rb") as handle:
            return b"kimi_cli" in handle.read(4096)
    except OSError:
        return False


def default_kimi_binary() -> str:
    """Locate the Kimi Code executable.

    The native ``~/.kimi-code/bin/kimi`` wins over anything on PATH: a stale
    ``kimi`` from the deprecated Python ``kimi-cli`` may shadow it, and that
    build exposes a much smaller ACP surface.
    """
    override = os.environ.get("COOMI_KIMI_BIN", "").strip()
    if override:
        return override

    for cand in ["~/.kimi-code/bin/kimi", "/usr/local/bin/kimi", "/opt/kimi-code/bin/kimi"]:
        path = _first_existing([cand])
        if path and not _looks_like_legacy_shim(path):
            return path

    found = shutil.which("kimi")
    if found and not _looks_like_legacy_shim(found):
        return found
    return ""


@dataclass(slots=True)
class Settings:
    kimi_bin: str = field(default_factory=default_kimi_binary)
    acp_args: tuple[str, ...] = ("acp",)
    workspace: Path = field(default_factory=lambda: WORKSPACE)
    additional_dirs: tuple[str, ...] = ()
    default_model: str = field(
        default_factory=lambda: os.environ.get("COOMI_KIMI_MODEL", "")
    )
    default_mode: str = field(
        default_factory=lambda: os.environ.get("COOMI_KIMI_MODE", "default")
    )
    default_thinking: str = field(
        default_factory=lambda: os.environ.get("COOMI_KIMI_THINKING", "")
    )
    permission_policy: str = field(
        default_factory=lambda: os.environ.get("COOMI_KIMI_PERMISSION", "auto-safe")
    )
    # Loopback by default: the host exposes file reads, shell execution and an
    # unauthenticated RPC surface, so LAN exposure has to be deliberate.
    host: str = field(default_factory=lambda: os.environ.get("COOMI_KIMI_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("COOMI_KIMI_PORT", "8765")))
    bridge_port: int = field(default_factory=lambda: int(
        os.environ.get("COOMI_KIMI_BRIDGE_PORT", str(int(os.environ.get("COOMI_KIMI_PORT", "8765")) + 1))
    ))
    auto_approve_within_seconds: int = field(
        default_factory=lambda: int(os.environ.get("COOMI_KIMI_AUTO_APPROVE_TIMEOUT", "0"))
    )
    max_history_turns: int = 200
    mcp_config_path: Path = field(default_factory=lambda: CONFIG_DIR / "mcp.json")
    agent_profile_path: Path = field(default_factory=lambda: CONFIG_DIR / "coomi-agent.md")

    def kimi_env(self) -> dict[str, str]:
        env = dict(os.environ)
        bin_dir = str(Path(self.kimi_bin).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "xterm-256color")
        return env

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kimi_bin": self.kimi_bin,
            "workspace": str(self.workspace),
            "additional_dirs": list(self.additional_dirs),
            "default_model": self.default_model,
            "default_mode": self.default_mode,
            "default_thinking": self.default_thinking,
            "permission_policy": self.permission_policy,
            "host": self.host,
            "port": self.port,
            "bridge_port": self.bridge_port,
            "version": VERSION,
        }


def load_settings() -> Settings:
    settings = Settings()
    settings.workspace.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return settings



# --------------------------------------------------------------------------
# Provider configuration — what Kimi Code itself reads
# --------------------------------------------------------------------------
#
# Written against vendor/docs/kimi-code/en_configuration_{providers,config-files,
# env-vars,overrides}.md. The facts that matter:
#
# * Credentials are NEVER taken from the shell. They live in
#   `$KIMI_CODE_HOME/config.toml`, resolved as: `[providers.<n>].api_key` >
#   `[providers.<n>.env].<PROVIDER>_API_KEY` > startup error.
# * Provider types are exactly: kimi, anthropic, openai, openai_responses,
#   google-genai, vertexai. (`google`/`vertex` are not valid names.)
# * TOML bare keys are [A-Za-z0-9_-] only, so names containing `.` `:` `/` must
#   be quoted: [providers."managed:kimi-code"], [models."kimi-code/k3"].
# * `max_context_size` is required on a model alias.
# * Automatic compaction is `[loop_control] reserved_context_size`: it triggers
#   when the *remaining* window falls below it — "compact at 90%" therefore
#   means reserving 10% of max_context_size.
# * `KIMI_MODEL_*` is the only credential channel read from the environment; it
#   outranks default_model but is never persisted. Used for one-off overrides.

KIMI_CODE_HOME = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code")))

PROVIDER_TYPES: dict[str, dict[str, str]] = {
    "kimi": {"key": "KIMI_API_KEY", "base": "KIMI_BASE_URL", "default_base_url": "https://api.moonshot.ai/v1"},
    "anthropic": {"key": "ANTHROPIC_API_KEY", "base": "ANTHROPIC_BASE_URL", "default_base_url": ""},
    "openai": {"key": "OPENAI_API_KEY", "base": "OPENAI_BASE_URL", "default_base_url": "https://api.openai.com/v1"},
    "openai_responses": {"key": "OPENAI_API_KEY", "base": "OPENAI_BASE_URL", "default_base_url": "https://api.openai.com/v1"},
    "google-genai": {"key": "GOOGLE_API_KEY", "base": "GOOGLE_GEMINI_BASE_URL", "default_base_url": ""},
    "vertexai": {"key": "VERTEXAI_API_KEY", "base": "GOOGLE_VERTEX_BASE_URL", "default_base_url": ""},
}

CAPABILITIES = ("thinking", "always_thinking", "image_in", "video_in", "audio_in", "tool_use")
PERMISSION_MODES = ("manual", "yolo", "auto")
THINKING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MANAGED_PROVIDER = "managed:kimi-code"


def provider_config_path() -> Path:
    return KIMI_CODE_HOME / "config.toml"


def _fmt_value(value: Any) -> str:
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _fmt_key(name: str) -> str:
    import re

    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _fmt_value(name)


def _header_parts(inner: str) -> tuple[str, ...]:
    """Split a TOML header body `a."b/c".d` into parts, honouring quotes."""
    parts: list[str] = []
    buf = ""
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == '"':
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
            j = i + 1
            while j < len(inner) and inner[j] != '"':
                j += 2 if inner[j] == "\\" else 1
            parts.append(inner[i + 1 : j])
            i = j + 1
        elif ch == ".":
            parts.append(buf.strip())
            buf = ""
            i += 1
        else:
            buf += ch
            i += 1
    if buf.strip():
        parts.append(buf.strip())
    return tuple(p for p in parts if p != "")


class TomlBlocks:
    """config.toml as ordered blocks, so unrelated content always survives.

    Root scalars are kept separately and re-emitted first, because TOML requires
    them to precede the first table header.
    """

    def __init__(self) -> None:
        self.root: list[str] = []
        self.blocks: list[tuple[tuple[str, ...], bool, list[str]]] = []  # (name, is_array, lines)

    @classmethod
    def parse(cls, text: str) -> "TomlBlocks":
        doc = cls()
        current: list[str] = doc.root
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                is_array = stripped.startswith("[[")
                inner = stripped[2:-2] if is_array else stripped[1:-1]
                name = _header_parts(inner.strip())
                if name:
                    block_lines: list[str] = []
                    doc.blocks.append((name, is_array, block_lines))
                    current = block_lines
                    current.append(line)
                    continue
            current.append(line)
        return doc

    def block(self, name: tuple[str, ...]) -> list[str] | None:
        for nm, _, lines in self.blocks:
            if nm == name:
                return lines
        return None

    def descendants(self, name: tuple[str, ...]) -> list[tuple[tuple[str, ...], bool, list[str]]]:
        return [(nm, arr, ln) for nm, arr, ln in self.blocks
                if nm != name and len(nm) > len(name) and nm[: len(name)] == name]

    def remove(self, name: tuple[str, ...], with_subtables: bool = True) -> None:
        doomed = {id(ln) for _, _, ln in self.descendants(name)} if with_subtables else set()
        doomed |= {id(ln) for nm, _, ln in self.blocks if nm == name}
        self.blocks = [b for b in self.blocks if id(b[2]) not in doomed]

    def set_scalar(self, key: str, value: Any) -> None:
        self.drop_scalar(key)
        self.root.append(f"{key} = {_fmt_value(value)}")

    def drop_scalar(self, key: str) -> None:
        import re

        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        self.root = [ln for ln in self.root if not pattern.match(ln)]
        # a stray scalar after a header would be invalid TOML; it belongs to a table
        for _, _, lines in self.blocks:
            lines[:] = [ln for ln in lines if not pattern.match(ln)]

    def upsert_table(self, name: tuple[str, ...], lines: list[str], replace: bool = True) -> None:
        """Insert or replace `[name]` plus its body; sub-tables of the old one go too."""
        if replace:
            self.remove(name)
        body = [f"[{_fmt_key(name[0])}]" if len(name) == 1
                else "[" + ".".join(_fmt_key(p) for p in name) + "]"] + lines
        self.blocks.append((name, False, body))

    def render(self) -> str:
        chunks: list[str] = []
        root = [ln for ln in self.root if ln.strip()]
        if root:
            chunks.append("\n".join(root))
        for _, _, lines in self.blocks:
            body = "\n".join(lines).strip("\n")
            if body.strip():
                chunks.append(body)
        return "\n\n".join(chunks).rstrip("\n") + "\n"


def parse_provider_config(path: Path | None = None) -> dict[str, Any]:
    """Read config.toml with a real TOML parser. Never raises."""
    import tomllib

    target = path or provider_config_path()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"exists": False, "path": str(target), "providers": [], "models": {}, "error": ""}
    except (OSError, ValueError) as exc:
        return {"exists": target.is_file(), "path": str(target), "providers": [], "models": {},
                "error": str(exc)}
    providers = []
    for name, body in (data.get("providers") or {}).items():
        if not isinstance(body, dict):
            continue
        env = body.get("env") or {}
        spec = PROVIDER_TYPES.get(body.get("type", ""), {})
        key = body.get("api_key") or ""
        providers.append({
            "name": name,
            "type": body.get("type", ""),
            "base_url": body.get("base_url") or env.get(spec.get("base", ""), ""),
            "has_key": bool(key) or any(k.endswith("_API_KEY") and v for k, v in env.items()),
            "oauth": bool(body.get("oauth")),
            "managed": name == MANAGED_PROVIDER,
        })
    models = {}
    for alias, body in (data.get("models") or {}).items():
        if isinstance(body, dict):
            models[alias] = {
                "provider": body.get("provider", ""),
                "model": body.get("model", ""),
                "max_context_size": body.get("max_context_size"),
                "capabilities": list(body.get("capabilities") or []),
            }
    loop = data.get("loop_control") or {}
    thinking = data.get("thinking") or {}
    return {
        "exists": True,
        "path": str(target),
        "providers": providers,
        "models": models,
        "default_model": data.get("default_model", ""),
        "default_permission_mode": data.get("default_permission_mode", ""),
        "telemetry": data.get("telemetry"),
        "reserved_context_size": loop.get("reserved_context_size"),
        "max_attempts_per_step": loop.get("max_attempts_per_step"),
        "thinking_enabled": thinking.get("enabled"),
        "thinking_effort": thinking.get("effort", ""),
        "error": "",
    }


def _validate_provider_args(**kw: Any) -> None:
    if kw["ptype"] not in PROVIDER_TYPES:
        raise ValueError(f"unknown provider type {kw['ptype']!r}; expected one of {sorted(PROVIDER_TYPES)}")
    if not kw["model"].strip():
        raise ValueError("model is required")
    if not isinstance(kw["max_context_size"], int) or kw["max_context_size"] < 1:
        raise ValueError("max_context_size must be a positive integer")
    bad = [c for c in kw["capabilities"] if c not in CAPABILITIES]
    if bad:
        raise ValueError(f"unknown capabilities {bad}; expected from {list(CAPABILITIES)}")
    if kw["permission_mode"] and kw["permission_mode"] not in PERMISSION_MODES:
        raise ValueError(f"permission_mode must be one of {list(PERMISSION_MODES)}")
    if kw["thinking_effort"] and kw["thinking_effort"] not in THINKING_EFFORTS:
        raise ValueError(f"thinking_effort must be one of {list(THINKING_EFFORTS)}")
    if not (0 <= kw["compact_at_percent"] <= 99):
        raise ValueError("compact_at_percent must be 0 (off) or 1..99")


def write_provider_config(
    *,
    name: str = "custom",
    ptype: str = "openai",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_context_size: int = 204800,
    capabilities: tuple[str, ...] | list[str] = ("thinking", "tool_use"),
    display_name: str = "",
    compact_at_percent: int = 90,
    permission_mode: str = "",
    thinking_enabled: bool | None = None,
    thinking_effort: str = "",
    use_env_subtable: bool = False,
    telemetry_off: bool = True,
    set_as_default: bool = True,
) -> dict[str, Any]:
    """Upsert `[providers.<name>]` + `[models."<name>/<model>"]` into config.toml.

    Everything we do not own — the OAuth managed provider, [[permission.rules]],
    hooks, services, other providers — is preserved verbatim, because the file is
    edited as blocks rather than regenerated. The result is parsed back with
    tomllib before replacing the live file, and a .bak of the previous version is
    kept, so a bad render can never brick a working setup.
    """
    name = name.strip() or "custom"
    model = model.strip()
    capabilities = list(capabilities)
    _validate_provider_args(ptype=ptype, model=model, max_context_size=max_context_size,
                            capabilities=capabilities, permission_mode=permission_mode,
                            thinking_effort=thinking_effort, compact_at_percent=compact_at_percent)

    alias = f"{name}/{model}"
    path = provider_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = TomlBlocks()
    if path.is_file():
        before = parse_provider_config(path)
        if before.get("error"):
            raise ValueError(f"existing config.toml does not parse: {before['error']}")
        for prov in before.get("providers", []):
            if prov["name"] == name and prov["oauth"]:
                raise ValueError(
                    f"provider {name!r} is an OAuth/managed login; manage it with `kimi /login` "
                    "or pick a different provider name"
                )
        doc = TomlBlocks.parse(path.read_text(encoding="utf-8"))

    spec = PROVIDER_TYPES[ptype]
    doc.remove(("providers", name))
    if use_env_subtable:
        # Credential via the `env` fallback sub-table (config file only, never shell).
        doc.upsert_table(("providers", name), [f"type = {_fmt_value(ptype)}"])
        env_lines = [f"{spec['key']} = {_fmt_value(api_key)}"]
        if base_url.strip():
            env_lines.append(f"{spec['base']} = {_fmt_value(base_url.strip())}")
        doc.blocks.append(((("providers", name, "env")), False,
                           [f"[providers.{_fmt_key(name)}.env]"] + env_lines))
    else:
        lines = [f"type = {_fmt_value(ptype)}"]
        if base_url.strip():
            lines.append(f"base_url = {_fmt_value(base_url.strip())}")
        if api_key.strip():
            lines.append(f"api_key = {_fmt_value(api_key.strip())}")
        doc.upsert_table(("providers", name), lines)

    model_lines = [
        f"provider = {_fmt_value(name)}",
        f"model = {_fmt_value(model)}",
        f"max_context_size = {int(max_context_size)}",
    ]
    if capabilities:
        model_lines.append(f"capabilities = {_fmt_value(capabilities)}")
    if display_name.strip():
        model_lines.append(f"display_name = {_fmt_value(display_name.strip())}")
    doc.upsert_table(("models", alias), model_lines)

    if compact_at_percent:
        reserved = max(1, int(round(max_context_size * (100 - compact_at_percent) / 100.0)))
        existing = doc.block(("loop_control",)) or []
        doc.remove(("loop_control",))
        keep = [ln for ln in existing
                if ln.strip() and not ln.strip().startswith("[")
                and "reserved_context_size" not in ln and not ln.strip().startswith("#")]
        doc.upsert_table(("loop_control",), keep + [f"reserved_context_size = {reserved}"])

    if thinking_enabled is not None or thinking_effort:
        lines = []
        if thinking_enabled is not None:
            lines.append(f"enabled = {_fmt_value(bool(thinking_enabled))}")
        if thinking_effort:
            lines.append(f"effort = {_fmt_value(thinking_effort)}")
        doc.upsert_table(("thinking",), lines)

    if set_as_default:
        doc.set_scalar("default_model", alias)
    if permission_mode:
        doc.set_scalar("default_permission_mode", permission_mode)
    if telemetry_off:
        doc.set_scalar("telemetry", False)

    rendered = doc.render()
    import tomllib

    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"internal error: rendered config.toml does not parse: {exc}") from exc

    previous = path.read_text(encoding="utf-8") if path.is_file() else ""
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # holds a credential
    except OSError:
        pass
    try:
        tomllib.loads(tmp.read_text(encoding="utf-8"))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    if previous:
        try:
            Path(str(path) + ".bak").write_text(previous, encoding="utf-8")
        except OSError:
            pass

    summary = read_provider_summary()
    summary["alias"] = alias
    summary["written"] = True
    return summary


def read_provider_summary() -> dict[str, Any]:
    """Public view of the live config: which provider is active — never the key."""
    parsed = parse_provider_config()
    default_alias = parsed.get("default_model", "")
    models = parsed.get("models", {})
    entry = models.get(default_alias, {})
    active = entry.get("provider") or (default_alias.split("/", 1)[0] if default_alias else "")
    prov = next((p for p in parsed.get("providers", []) if p["name"] == active), None)
    max_ctx = entry.get("max_context_size") or 0
    reserved = parsed.get("reserved_context_size") or 0
    return {
        "configured": bool(prov and prov.get("has_key")),
        "parse_error": parsed.get("error", ""),
        "path": parsed.get("path", ""),
        "exists": parsed.get("exists", False),
        "provider": active,
        "provider_type": (prov or {}).get("type", ""),
        "base_url": (prov or {}).get("base_url", ""),
        "has_key": (prov or {}).get("has_key", False),
        "oauth_login": any(p.get("oauth") for p in parsed.get("providers", [])),
        "managed_available": any(p.get("managed") for p in parsed.get("providers", [])),
        "model": entry.get("model") or (default_alias.split("/", 1)[1] if "/" in default_alias else default_alias),
        "default_model": default_alias,
        "max_context_size": max_ctx,
        "capabilities": entry.get("capabilities", []),
        "permission_mode": parsed.get("default_permission_mode", ""),
        "reserved_context_size": reserved,
        "compact_at_percent": int(round(100 - (reserved * 100.0 / max_ctx))) if reserved and max_ctx else 0,
        "telemetry": parsed.get("telemetry"),
        "thinking_enabled": parsed.get("thinking_enabled"),
        "thinking_effort": parsed.get("thinking_effort", ""),
        "providers": parsed.get("providers", []),
        "model_aliases": sorted(models),
        "provider_types": sorted(PROVIDER_TYPES),
        "capabilities_allowed": list(CAPABILITIES),
        "permission_modes": list(PERMISSION_MODES),
        "kimi_code_home": str(KIMI_CODE_HOME),
        "kimi_bin": default_kimi_binary(),
    }


def kimi_model_env() -> dict[str, str]:
    """`KIMI_MODEL_*` — the only credential channel read from the environment.

    Empty unless both NAME and API_KEY are set, so config.toml remains the normal
    path. Nothing is ever written back to the file by this channel.
    """
    name = os.environ.get("KIMI_MODEL_NAME", "").strip()
    key = os.environ.get("KIMI_MODEL_API_KEY", "").strip()
    if not (name and key):
        return {}
    env = {"KIMI_MODEL_NAME": name, "KIMI_MODEL_API_KEY": key}
    for var in ("KIMI_MODEL_PROVIDER_TYPE", "KIMI_MODEL_BASE_URL", "KIMI_MODEL_MAX_CONTEXT_SIZE",
                "KIMI_MODEL_CAPABILITIES", "KIMI_MODEL_DISPLAY_NAME", "KIMI_MODEL_THINKING_EFFORT",
                "KIMI_MODEL_MAX_OUTPUT_SIZE", "KIMI_MODEL_REASONING_KEY", "KIMI_MODEL_ADAPTIVE_THINKING"):
        val = os.environ.get(var, "").strip()
        if val:
            env[var] = val
    return env
