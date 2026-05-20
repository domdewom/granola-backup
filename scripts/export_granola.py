#!/usr/bin/env python3
"""Incremental Granola backup exporter.

Exports per meeting:
- notes.md / notes.json
- enhanced.md / enhanced.json
- transcript.md / transcript.json
- meeting.json (metadata snapshot)

State is tracked in backups/manifests/sync_state.json.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
except Exception:  # pragma: no cover
    Progress = None

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


API_BASE_DEFAULT = "https://api.granola.ai"
PUBLIC_API_BASE_DEFAULT = "https://public-api.granola.ai"
WORKOS_AUTH_URL = "https://api.workos.com/user_management/authenticate"
DEFAULT_CLIENT_ID = "client_GranolaMac"
APP_VERSION = "7.0.0"
PAGE_SIZE = 100
PUBLIC_API_PAGE_SIZE = 30
MAX_PAGES = 1000
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
UNFILED_DIR_NAME = "_unfiled"


@dataclasses.dataclass
class RetryConfig:
    max_attempts: int = 6
    backoff_seconds: list[int] = dataclasses.field(default_factory=lambda: [2, 5, 15, 30, 60])


@dataclasses.dataclass
class HealthGuardConfig:
    drop_threshold_percent: int = 60
    min_expected_items: int = 3


@dataclasses.dataclass
class ExportConfig:
    max_requests_per_second: float = 2.0
    workspace_id: str | None = None
    retry: RetryConfig = dataclasses.field(default_factory=RetryConfig)
    health_guard: HealthGuardConfig = dataclasses.field(default_factory=HealthGuardConfig)


@dataclasses.dataclass
class PhaseState:
    name: str
    total: int
    current: int = 0
    detail: str = ""
    started_at: float = dataclasses.field(default_factory=time.time)


class BaseProgressReporter:
    def __init__(self, log_every: int = 25) -> None:
        self.log_every = max(1, log_every)
        self.active: PhaseState | None = None

    def start_phase(self, name: str, total: int) -> None:
        self.active = PhaseState(name=name, total=max(1, total))
        self._on_start(self.active)

    def advance(self, step: int = 1, detail: str | None = None) -> None:
        if not self.active:
            return
        self.active.current = min(self.active.total, self.active.current + step)
        if detail is not None:
            self.active.detail = detail
        self._on_advance(self.active)

    def finish_phase(self, summary: str = "") -> None:
        if not self.active:
            return
        self.active.current = self.active.total
        self._on_finish(self.active, summary)
        self.active = None

    def close(self) -> None:
        return

    def _rate_eta(self, st: PhaseState) -> tuple[float, str]:
        elapsed = max(0.001, time.time() - st.started_at)
        rate = st.current / elapsed
        if rate <= 0:
            return 0.0, "--"
        remaining = max(0, st.total - st.current)
        eta_seconds = int(remaining / rate)
        mins, secs = divmod(eta_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            eta = f"{hours}h{mins:02d}m"
        elif mins > 0:
            eta = f"{mins}m{secs:02d}s"
        else:
            eta = f"{secs}s"
        return rate, eta

    def _truncate(self, text: str, max_len: int = 80) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def _format_line(self, st: PhaseState) -> str:
        pct = (st.current / st.total) * 100
        rate, eta = self._rate_eta(st)
        base = f"{st.current}/{st.total} ({pct:5.1f}%) | {rate:4.2f}/s | ETA {eta}"
        if st.detail:
            base += f" | {self._truncate(st.detail)}"
        return base

    def _on_start(self, st: PhaseState) -> None:
        raise NotImplementedError

    def _on_advance(self, st: PhaseState) -> None:
        raise NotImplementedError

    def _on_finish(self, st: PhaseState, summary: str) -> None:
        raise NotImplementedError


class NullProgressReporter(BaseProgressReporter):
    def _on_start(self, st: PhaseState) -> None:
        return

    def _on_advance(self, st: PhaseState) -> None:
        return

    def _on_finish(self, st: PhaseState, summary: str) -> None:
        return


class PlainProgressReporter(BaseProgressReporter):
    def _on_start(self, st: PhaseState) -> None:
        print(f"[{st.name}] start total={st.total}")

    def _on_advance(self, st: PhaseState) -> None:
        if st.current == st.total or st.current % self.log_every == 0:
            print(f"[{st.name}] {self._format_line(st)}")

    def _on_finish(self, st: PhaseState, summary: str) -> None:
        line = self._format_line(st)
        if summary:
            line = f"{line} | {summary}"
        print(f"[{st.name}] done {line}")


class RichProgressReporter(BaseProgressReporter):
    def __init__(self, log_every: int = 25) -> None:
        super().__init__(log_every=log_every)
        self.progress = (
            Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("{task.fields[stats]}"),
                TextColumn("{task.fields[detail]}"),
                transient=False,
            )
            if Progress
            else None
        )
        self.task_id: int | None = None
        if self.progress:
            self.progress.start()

    def _on_start(self, st: PhaseState) -> None:
        if not self.progress:
            return
        self.task_id = self.progress.add_task(st.name, total=st.total, stats="", detail="")

    def _on_advance(self, st: PhaseState) -> None:
        if not self.progress or self.task_id is None:
            return
        rate, eta = self._rate_eta(st)
        stats = f"{st.current}/{st.total} | {rate:4.2f}/s | ETA {eta}"
        detail = self._truncate(st.detail, max_len=90) if st.detail else ""
        self.progress.update(self.task_id, completed=st.current, stats=stats, detail=detail)

    def _on_finish(self, st: PhaseState, summary: str) -> None:
        if not self.progress or self.task_id is None:
            return
        self._on_advance(st)
        detail = summary if summary else st.detail
        self.progress.update(self.task_id, detail=self._truncate(detail, max_len=90))
        self.task_id = None

    def close(self) -> None:
        if self.progress:
            self.progress.stop()


def create_progress_reporter() -> BaseProgressReporter:
    mode = os.getenv("PROGRESS_MODE", "auto").strip().lower()
    log_every = int(os.getenv("PROGRESS_LOG_EVERY", "25"))
    if mode not in {"auto", "rich", "plain", "off"}:
        mode = "auto"

    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    is_tty = sys.stdout.isatty()

    if mode == "off":
        return NullProgressReporter(log_every=log_every)

    if is_ci and mode == "auto":
        return NullProgressReporter(log_every=log_every)

    use_rich = mode == "rich" or (mode == "auto" and is_tty and not is_ci)
    if use_rich and Progress is None:
        print("rich is unavailable, falling back to plain progress")
        use_rich = False

    if use_rich:
        return RichProgressReporter(log_every=log_every)
    return PlainProgressReporter(log_every=log_every)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    t = ts.strip()
    if not t:
        return None
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def safe_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def is_effectively_empty_export(content: str) -> bool:
    return strip_frontmatter(content).strip() in {"", "null", "[]", "{}"}


def safe_write_export(path: Path, content: str, allow_empty_overwrite: bool = False) -> tuple[bool, bool]:
    """Return (changed, skipped_empty_overwrite)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if (
        not allow_empty_overwrite
        and path.exists()
        and not is_effectively_empty_export(existing)
        and is_effectively_empty_export(content)
    ):
        return False, True
    return safe_write(path, content), False


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sanitize_title_for_folder(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "Untitled"


def meeting_folder_name(created_at: str | None, title: str | None) -> str:
    parsed = parse_iso(created_at)
    date_part = parsed.strftime("%Y%m%d") if parsed else "00000000"
    title_part = sanitize_title_for_folder(title or "Untitled")
    return f"{date_part}_{title_part}"


def folder_dir_name(folder: dict[str, Any]) -> str:
    """Sanitized directory name for a Granola folder. Collision handling is the caller's job."""
    return sanitize_title_for_folder(str((folder or {}).get("name") or "Untitled"))


def primary_folder(membership: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Pick the primary folder for a note. Lowest folder id wins so the result is stable
    across runs even if Granola reorders the membership list."""
    folders = [f for f in (membership or []) if isinstance(f, dict) and f.get("id")]
    if not folders:
        return None
    return sorted(folders, key=lambda f: str(f.get("id")))[0]


def build_folder_dir_map(memberships: list[list[dict[str, Any]]]) -> dict[str, str]:
    """Map folder id → directory name across all memberships in a run.
    Two distinct folder ids that sanitize to the same name get a `--<id8>` suffix."""
    by_id: dict[str, dict[str, Any]] = {}
    for membership in memberships:
        for f in membership or []:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("id") or "")
            if fid and fid not in by_id:
                by_id[fid] = f

    grouped: dict[str, list[str]] = {}
    for fid, f in by_id.items():
        grouped.setdefault(folder_dir_name(f), []).append(fid)

    result: dict[str, str] = {}
    for name, ids in grouped.items():
        if len(ids) == 1:
            result[ids[0]] = name
        else:
            for fid in ids:
                result[fid] = f"{name}--{fid[:8]}"
    return result


def desired_primary_dir(membership: list[dict[str, Any]] | None, folder_dir_by_id: dict[str, str]) -> str:
    """Return the primary directory name (folder dir or _unfiled) for a meeting's membership."""
    pf = primary_folder(membership)
    if pf is None:
        return UNFILED_DIR_NAME
    fid = str(pf.get("id") or "")
    return folder_dir_by_id.get(fid) or folder_dir_name(pf)


def _yaml_fallback_dump(data: dict[str, Any]) -> str:
    # Minimal YAML writer used only if PyYAML isn't importable. Keeps tests + first-run usable.
    def encode(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value)
        if s == "" or any(c in s for c in ":#\n\"'[]{},&*!|>%@`") or s.strip() != s:
            return json.dumps(s, ensure_ascii=False)
        return s

    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    inline = ", ".join(f"{k}: {encode(v)}" for k, v in item.items())
                    lines.append(f"  - {{ {inline} }}")
                else:
                    lines.append(f"  - {encode(item)}")
        elif isinstance(value, dict):
            inline = ", ".join(f"{k}: {encode(v)}" for k, v in value.items())
            lines.append(f"{key}: {{ {inline} }}")
        else:
            lines.append(f"{key}: {encode(value)}")
    return "\n".join(lines) + "\n"


def build_frontmatter(meta: dict[str, Any], file_kind: str) -> str:
    """YAML frontmatter block (with trailing blank line) prepended to each exported .md file."""
    fm: dict[str, Any] = {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "folder_primary": meta.get("folder_primary") or UNFILED_DIR_NAME,
        "folders": [
            {"id": f.get("id"), "name": f.get("name")}
            for f in (meta.get("folder_membership") or [])
            if isinstance(f, dict)
        ],
        "file_kind": file_kind,
    }
    if meta.get("web_url"):
        fm["web_url"] = meta["web_url"]
    if meta.get("attendees"):
        fm["attendees"] = meta["attendees"]
    if meta.get("provider"):
        fm["provider"] = meta["provider"]

    if yaml is not None:
        body = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    else:
        body = _yaml_fallback_dump(fm)
    return f"---\n{body}---\n\n"


_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n(?:\r?\n)?", re.DOTALL)


def strip_frontmatter(content: str) -> str:
    return _FRONTMATTER_RE.sub("", content, count=1)


def load_config(path: Path) -> ExportConfig:
    cfg = ExportConfig()
    if not path.exists() or yaml is None:
        return cfg

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.max_requests_per_second = float(raw.get("max_requests_per_second", cfg.max_requests_per_second))
    cfg.workspace_id = raw.get("workspace_id") or None

    retry = raw.get("retry") or {}
    cfg.retry = RetryConfig(
        max_attempts=int(retry.get("max_attempts", cfg.retry.max_attempts)),
        backoff_seconds=[int(x) for x in retry.get("backoff_seconds", cfg.retry.backoff_seconds)],
    )

    hg = raw.get("health_guard") or {}
    cfg.health_guard = HealthGuardConfig(
        drop_threshold_percent=int(hg.get("drop_threshold_percent", cfg.health_guard.drop_threshold_percent)),
        min_expected_items=int(hg.get("min_expected_items", cfg.health_guard.min_expected_items)),
    )
    return cfg


def _parse_nested_json(val: Any) -> dict[str, Any] | None:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def parse_supabase_credentials(raw_json: str, env_client_id: str | None = None) -> dict[str, str]:
    data = json.loads(raw_json)

    workos = _parse_nested_json(data.get("workos_tokens"))
    if workos and (workos.get("access_token") or workos.get("refresh_token")):
        return {
            "access_token": str(workos.get("access_token") or ""),
            "refresh_token": str(workos.get("refresh_token") or ""),
            "client_id": str(workos.get("client_id") or env_client_id or DEFAULT_CLIENT_ID),
        }

    cognito = _parse_nested_json(data.get("cognito_tokens"))
    if cognito and (cognito.get("access_token") or cognito.get("refresh_token")):
        return {
            "access_token": str(cognito.get("access_token") or ""),
            "refresh_token": str(cognito.get("refresh_token") or ""),
            "client_id": str(cognito.get("client_id") or env_client_id or DEFAULT_CLIENT_ID),
        }

    return {
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": str(data.get("refresh_token") or ""),
        "client_id": str(data.get("client_id") or env_client_id or DEFAULT_CLIENT_ID),
    }


def select_provider(api_key: str, supabase_json: str) -> str | None:
    if api_key.strip():
        return "official"
    if supabase_json.strip():
        return "internal"
    return None


class GranolaClient:
    def __init__(
        self,
        api_base: str,
        access_token: str,
        refresh_token: str,
        client_id: str,
        retry_cfg: RetryConfig,
        max_rps: float,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.retry_cfg = retry_cfg
        self.min_interval = 1.0 / max(max_rps, 0.1)
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-App-Version": APP_VERSION,
            "X-Client-Version": APP_VERSION,
            "X-Client-Type": "backup",
            "X-Client-Platform": "github-actions",
            "X-Client-Architecture": "x64",
            "X-Client-Id": "granola-backup-exporter",
            "User-Agent": "granola-backup-exporter/1.0",
        }

    def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise RuntimeError("Access token expired and no refresh token available")

        resp = requests.post(
            WORKOS_AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code}: {resp.text[:500]}")

        payload = resp.json()
        self.access_token = str(payload.get("access_token") or "")
        self.refresh_token = str(payload.get("refresh_token") or self.refresh_token)
        if not self.access_token:
            raise RuntimeError("Token refresh succeeded but no access_token returned")

    def post(self, endpoint: str, body: dict[str, Any]) -> Any:
        attempts = max(self.retry_cfg.max_attempts, 1)
        for attempt in range(attempts):
            self._throttle()
            self._last_request_at = time.time()
            try:
                resp = requests.post(
                    f"{self.api_base}{endpoint}",
                    headers=self._headers(),
                    json=body,
                    timeout=45,
                )
            except requests.RequestException as err:
                if attempt == attempts - 1:
                    raise RuntimeError(f"Network error POST {endpoint}: {err}") from err
                sleep_for = self.retry_cfg.backoff_seconds[min(attempt, len(self.retry_cfg.backoff_seconds) - 1)]
                time.sleep(sleep_for)
                continue

            if resp.status_code == 401:
                self._refresh_access_token()
                continue

            if resp.status_code in RETRYABLE_STATUSES:
                if attempt == attempts - 1:
                    raise RuntimeError(f"HTTP {resp.status_code} for {endpoint}: {resp.text[:500]}")
                sleep_for = self.retry_cfg.backoff_seconds[min(attempt, len(self.retry_cfg.backoff_seconds) - 1)]
                time.sleep(sleep_for)
                continue

            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} for {endpoint}: {resp.text[:500]}")

            return resp.json()

        raise RuntimeError(f"Unable to complete POST {endpoint}")

    def list_meetings(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0

        for _ in range(MAX_PAGES):
            body: dict[str, Any] = {
                "limit": PAGE_SIZE,
                "offset": offset,
                "include_last_viewed_panel": True,
            }
            if workspace_id:
                body["workspace_id"] = workspace_id

            data = self.post("/v2/get-documents", body)
            docs = (data or {}).get("docs") or []
            if not isinstance(docs, list) or not docs:
                break

            out.extend(docs)
            if len(docs) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        return out

    def get_metadata(self, meeting_id: str) -> dict[str, Any]:
        data = self.post("/v1/get-document-metadata", {"document_id": meeting_id})
        return data if isinstance(data, dict) else {}

    def get_transcript(self, meeting_id: str) -> list[dict[str, Any]]:
        data = self.post("/v1/get-document-transcript", {"document_id": meeting_id})
        return data if isinstance(data, list) else []


class OfficialGranolaClient:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        retry_cfg: RetryConfig,
        max_rps: float,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.retry_cfg = retry_cfg
        self.min_interval = 1.0 / max(max_rps, 0.1)
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "granola-backup-exporter/1.0",
        }

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        attempts = max(self.retry_cfg.max_attempts, 1)
        for attempt in range(attempts):
            self._throttle()
            self._last_request_at = time.time()
            try:
                resp = requests.get(
                    f"{self.api_base}{endpoint}",
                    headers=self._headers(),
                    params=params,
                    timeout=45,
                )
            except requests.RequestException as err:
                if attempt == attempts - 1:
                    raise RuntimeError(f"Network error GET {endpoint}: {err}") from err
                sleep_for = self.retry_cfg.backoff_seconds[min(attempt, len(self.retry_cfg.backoff_seconds) - 1)]
                time.sleep(sleep_for)
                continue

            if resp.status_code in RETRYABLE_STATUSES:
                if attempt == attempts - 1:
                    raise RuntimeError(f"HTTP {resp.status_code} for {endpoint}: {resp.text[:500]}")
                sleep_for = self.retry_cfg.backoff_seconds[min(attempt, len(self.retry_cfg.backoff_seconds) - 1)]
                time.sleep(sleep_for)
                continue

            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} for {endpoint}: {resp.text[:500]}")

            return resp.json()

        raise RuntimeError(f"Unable to complete GET {endpoint}")

    def list_meetings(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        if workspace_id:
            print("Warning: BACKUP_WORKSPACE_ID is ignored when using the official Granola API", file=sys.stderr)

        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"page_size": PUBLIC_API_PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor

            data = self.get("/v1/notes", params=params)
            notes = (data or {}).get("notes") or []
            if not isinstance(notes, list) or not notes:
                break

            out.extend(notes)
            if not data.get("hasMore"):
                break

            cursor = data.get("cursor")
            if not cursor:
                break

        return out

    def get_note(self, note_id: str) -> dict[str, Any]:
        data = self.get(f"/v1/notes/{note_id}", params={"include": "transcript"})
        return data if isinstance(data, dict) else {}


def _render_inline(node: dict[str, Any]) -> str:
    if node.get("type") == "text":
        txt = str(node.get("text") or "")
        for mark in node.get("marks") or []:
            mtype = (mark or {}).get("type")
            attrs = (mark or {}).get("attrs") or {}
            if mtype == "bold":
                txt = f"**{txt}**"
            elif mtype == "italic":
                txt = f"*{txt}*"
            elif mtype == "code":
                txt = f"`{txt}`"
            elif mtype == "link":
                href = attrs.get("href")
                if href:
                    txt = f"[{txt}]({href})"
        return txt

    if node.get("type") == "hardBreak":
        return "\\n"

    parts = [_render_inline(child) for child in (node.get("content") or [])]
    return "".join(parts)


def _render_blocks(nodes: list[dict[str, Any]], indent: int = 0) -> list[str]:
    lines: list[str] = []

    for node in nodes:
        ntype = node.get("type")
        content = node.get("content") or []

        if ntype == "heading":
            level = int((node.get("attrs") or {}).get("level", 1))
            level = max(1, min(level, 6))
            txt = "".join(_render_inline(c) for c in content).strip()
            lines.extend([f"{'#' * level} {txt}".rstrip(), ""])
        elif ntype == "paragraph":
            txt = "".join(_render_inline(c) for c in content).strip()
            lines.extend([txt, ""])
        elif ntype == "bulletList":
            for item in content:
                item_lines = _render_blocks(item.get("content") or [], indent + 2)
                first = True
                for il in item_lines:
                    if not il:
                        continue
                    prefix = " " * indent + ("- " if first else "  ")
                    lines.append(prefix + il)
                    first = False
            lines.append("")
        elif ntype == "orderedList":
            idx = 1
            for item in content:
                item_lines = _render_blocks(item.get("content") or [], indent + 3)
                first = True
                for il in item_lines:
                    if not il:
                        continue
                    prefix = " " * indent + (f"{idx}. " if first else "   ")
                    lines.append(prefix + il)
                    first = False
                idx += 1
            lines.append("")
        elif ntype == "listItem":
            lines.extend(_render_blocks(content, indent))
        elif ntype == "blockquote":
            q_lines = _render_blocks(content, indent)
            for q in q_lines:
                if q:
                    lines.append(f"> {q}")
            lines.append("")
        elif ntype == "codeBlock":
            code = "\n".join(
                "".join(_render_inline(c) for c in ((child or {}).get("content") or [])) for child in content
            )
            lines.extend(["```", code, "```", ""])
        else:
            txt = "".join(_render_inline(c) for c in content).strip()
            if txt:
                lines.extend([txt, ""])

    return lines


def prosemirror_to_markdown(doc: dict[str, Any] | None) -> str:
    if not doc or not isinstance(doc, dict):
        return ""
    nodes = doc.get("content") or []
    lines = _render_blocks(nodes)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out + "\n" if out else ""


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"</?[a-zA-Z][^>]*>", value))


def _normalize_markdown(text: str) -> str:
    lines: list[str] = []
    blank = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip():
            lines.append(line)
            blank = False
        elif not blank and lines:
            lines.append("")
            blank = True
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out + "\n" if out else ""


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[dict[str, int | str]] = []
        self.link_stack: list[tuple[str, int]] = []
        self.in_pre = False

    def _text(self) -> str:
        return "".join(self.parts)

    def _endswith(self, suffix: str) -> bool:
        return self._text().endswith(suffix)

    def _append(self, value: str) -> None:
        self.parts.append(value)

    def _block_break(self) -> None:
        if not self.parts:
            return
        if self._endswith("\n\n"):
            return
        if self._endswith("\n"):
            self._append("\n")
        else:
            self._append("\n\n")

    def _line_break(self) -> None:
        if not self._endswith("\n"):
            self._append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value for key, value in attrs}
        if tag in {"p", "div", "section", "article"}:
            self._block_break()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._block_break()
            level = int(tag[1])
            self._append(f"{'#' * level} ")
        elif tag == "br":
            self._line_break()
        elif tag == "hr":
            self._block_break()
            self._append("---")
            self._block_break()
        elif tag == "ul":
            self._block_break()
            self.list_stack.append({"type": "ul", "index": 0})
        elif tag == "ol":
            self._block_break()
            self.list_stack.append({"type": "ol", "index": 0})
        elif tag == "li":
            self._line_break()
            indent = "  " * max(len(self.list_stack) - 1, 0)
            if self.list_stack and self.list_stack[-1]["type"] == "ol":
                self.list_stack[-1]["index"] = int(self.list_stack[-1]["index"]) + 1
                prefix = f"{self.list_stack[-1]['index']}. "
            else:
                prefix = "- "
            self._append(indent + prefix)
        elif tag in {"strong", "b"}:
            self._append("**")
        elif tag in {"em", "i"}:
            self._append("*")
        elif tag == "code" and not self.in_pre:
            self._append("`")
        elif tag == "pre":
            self._block_break()
            self._append("```\n")
            self.in_pre = True
        elif tag == "blockquote":
            self._block_break()
            self._append("> ")
        elif tag == "a":
            self.link_stack.append((attrs_dict.get("href") or "", len(self.parts)))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"strong", "b"}:
            self._append("**")
        elif tag in {"em", "i"}:
            self._append("*")
        elif tag == "code" and not self.in_pre:
            self._append("`")
        elif tag == "a" and self.link_stack:
            href, start = self.link_stack.pop()
            if href:
                label = "".join(self.parts[start:]).strip()
                self.parts[start:] = [f"[{label}]({href})" if label else href]
        elif tag == "pre":
            if not self._endswith("\n"):
                self._append("\n")
            self._append("```")
            self.in_pre = False
            self._block_break()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "article", "blockquote"}:
            self._block_break()
        elif tag == "li":
            self._line_break()
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._block_break()

    def handle_data(self, data: str) -> None:
        if self.in_pre:
            self._append(data)
            return
        text = re.sub(r"\s+", " ", html.unescape(data))
        if text:
            self._append(text)


def html_to_markdown(value: str) -> str:
    parser = _HTMLToMarkdownParser()
    parser.feed(value)
    parser.close()
    return _normalize_markdown("".join(parser.parts))


def enhanced_to_markdown(value: Any) -> str:
    if isinstance(value, dict):
        return prosemirror_to_markdown(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if _looks_like_html(text):
            return html_to_markdown(text)
        return _normalize_markdown(html.unescape(text))
    return ""


def panel_content(obj: dict[str, Any]) -> Any:
    panel = obj.get("last_viewed_panel")
    if not isinstance(panel, dict):
        return None
    return panel.get("content")


def enhanced_content_from(meeting: dict[str, Any], metadata: dict[str, Any]) -> Any:
    meeting_content = panel_content(meeting)
    if meeting_content is not None:
        return meeting_content
    return panel_content(metadata)


def summary_to_markdown(summary_markdown: Any, summary_text: Any) -> str:
    if isinstance(summary_markdown, str) and summary_markdown.strip():
        return _normalize_markdown(summary_markdown)
    if isinstance(summary_text, str) and summary_text.strip():
        return _normalize_markdown(summary_text)
    return ""


def transcript_source(seg: dict[str, Any]) -> str:
    if seg.get("source"):
        return str(seg.get("source") or "unknown")
    speaker = seg.get("speaker")
    if isinstance(speaker, dict):
        return str(speaker.get("name") or speaker.get("email") or speaker.get("source") or "unknown")
    return "unknown"


def transcript_to_markdown(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for seg in items:
        start_ts = parse_iso(seg.get("start_timestamp") or seg.get("start_time"))
        ts = start_ts.strftime("%H:%M:%S") if start_ts else "00:00:00"
        source = transcript_source(seg)
        text = str(seg.get("text") or "").strip()
        if text:
            lines.append(f"[{ts}][{source}] {text}")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def build_folders_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    folders: dict[str, dict[str, Any]] = {}
    unfiled: list[dict[str, Any]] = []
    for record in records:
        note_id = record.get("id")
        folder_name = record.get("folder_name")
        membership = record.get("folder_membership") or []
        if not membership:
            unfiled.append({"id": note_id, "folder_name": folder_name})
            continue
        for folder in membership:
            if not isinstance(folder, dict):
                continue
            fid = str(folder.get("id") or folder.get("name") or "unknown")
            item = folders.setdefault(
                fid,
                {
                    "id": folder.get("id"),
                    "object": folder.get("object"),
                    "name": folder.get("name"),
                    "note_count": 0,
                    "notes": [],
                },
            )
            item["note_count"] += 1
            item["notes"].append({"id": note_id, "folder_name": folder_name})

    ordered = sorted(folders.values(), key=lambda item: str(item.get("name") or item.get("id") or ""))
    return {
        "generated_at": utc_now_iso(),
        "folders": ordered,
        "unfiled": {"note_count": len(unfiled), "notes": unfiled},
    }


def reconcile_export_layout(
    prev_paths: dict[str, tuple[str, str]],
    desired_paths: dict[str, tuple[str, str]],
    md_root: Path,
    json_root: Path,
) -> list[dict[str, str]]:
    """Remove stale per-meeting directories when a meeting's primary folder changed.

    For each meeting id we previously exported, compare its previously-recorded
    `(primary_dir, leaf)` to the current desired pair. If different, delete the old
    location under both `md_root` and `json_root` and prune the parent if it becomes empty.

    Skipped — never deletes — when desired_paths has no entry for the meeting id.
    Callers pass an empty desired_paths to no-op the whole pass.
    """
    actions: list[dict[str, str]] = []
    for mid, prev in prev_paths.items():
        desired = desired_paths.get(mid)
        if not desired or desired == prev:
            continue
        prev_primary, prev_leaf = prev
        if not prev_leaf:
            continue
        for root in (md_root, json_root):
            old_dir = root / prev_primary / prev_leaf
            if old_dir.exists():
                shutil.rmtree(old_dir)
                parent = old_dir.parent
                if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
        actions.append(
            {
                "id": mid,
                "from": f"{prev_primary}/{prev_leaf}",
                "to": f"{desired[0]}/{desired[1]}",
            }
        )
    return actions


def select_incremental_meetings(
    meetings: list[dict[str, Any]],
    last_seen: dt.datetime | None,
    full_export: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for m in meetings:
        updated = parse_iso(m.get("updated_at"))
        if full_export or last_seen is None:
            selected.append(m)
        elif updated and updated > last_seen:
            selected.append(m)
    selected.sort(key=lambda m: m.get("updated_at") or "")
    return selected


def run() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if load_dotenv is not None:
        load_dotenv(repo_root / ".env", override=False)

    reporter = create_progress_reporter()
    try:
        backups_root = repo_root / "backups"
        md_root = backups_root / "granola-md"
        json_root = backups_root / "granola-json"
        manifest_root = backups_root / "manifests"
        manifest_root.mkdir(parents=True, exist_ok=True)

        config = load_config(repo_root / "backup.config.yaml")

        official_api_key = os.getenv("GRANOLA_API_KEY", "").strip()
        secret = os.getenv("GRANOLA_SUPABASE_JSON", "").strip()
        provider = select_provider(official_api_key, secret)

        if provider == "official":
            client: Any = OfficialGranolaClient(
                api_base=os.getenv("GRANOLA_PUBLIC_API_BASE") or PUBLIC_API_BASE_DEFAULT,
                api_key=official_api_key,
                retry_cfg=config.retry,
                max_rps=config.max_requests_per_second,
            )
        elif provider == "internal":
            if not secret:
                print("Error: GRANOLA_API_KEY is required. GRANOLA_SUPABASE_JSON is supported as a fallback.", file=sys.stderr)
                return 1

            env_client_id = os.getenv("GRANOLA_CLIENT_ID")
            creds = parse_supabase_credentials(secret, env_client_id=env_client_id)
            access_token = creds.get("access_token", "")
            refresh_token = creds.get("refresh_token", "")
            client_id = creds.get("client_id") or DEFAULT_CLIENT_ID
            if not access_token and not refresh_token:
                print("Error: Could not extract access/refresh token from GRANOLA_SUPABASE_JSON", file=sys.stderr)
                return 1

            client = GranolaClient(
                api_base=os.getenv("GRANOLA_API_BASE") or API_BASE_DEFAULT,
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                retry_cfg=config.retry,
                max_rps=config.max_requests_per_second,
            )
        else:
            print("Error: GRANOLA_API_KEY is required. GRANOLA_SUPABASE_JSON is supported as a fallback.", file=sys.stderr)
            return 1

        sync_state_path = manifest_root / "sync_state.json"
        prev_state = safe_read_json(sync_state_path)
        last_seen = parse_iso(prev_state.get("last_max_updated_at_seen"))
        prev_paths: dict[str, tuple[str, str]] = {
            str(k): (str(v[0]), str(v[1]))
            for k, v in (prev_state.get("paths_by_id") or {}).items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }

        reporter.start_phase("Discovery", 1)
        meetings = client.list_meetings(workspace_id=os.getenv("BACKUP_WORKSPACE_ID") or config.workspace_id)
        total_meetings = len(meetings)
        reporter.advance(detail=f"found {total_meetings} meetings")
        reporter.finish_phase(f"found {total_meetings} meetings")

        # The official listing endpoint (/v1/notes) doesn't include folder_membership —
        # that's only available via the per-note GET. So primary_dir is resolved inside
        # the export loop, after we fetch the note. desired_paths is built up there.
        desired_paths: dict[str, tuple[str, str]] = {}
        leaves_by_primary: dict[str, set[str]] = {}

        prev_total_meetings = int(prev_state.get("last_total_meetings") or 0)
        allow_drop = os.getenv("ALLOW_LARGE_DROP", "false").lower() == "true"
        drop_threshold = max(0, min(100, config.health_guard.drop_threshold_percent))
        if (
            not allow_drop
            and prev_total_meetings >= config.health_guard.min_expected_items
            and total_meetings < int(prev_total_meetings * (1 - drop_threshold / 100.0))
        ):
            raise RuntimeError(
                f"Health guard blocked run: total meetings dropped from {prev_total_meetings} to {total_meetings}. "
                "Set ALLOW_LARGE_DROP=true to override."
            )

        full_export = os.getenv("FULL_EXPORT", "false").lower() == "true"
        allow_empty_overwrite = os.getenv("ALLOW_EMPTY_CONTENT_OVERWRITE", "false").lower() == "true"
        reporter.start_phase("Selection", 1)
        selected = select_incremental_meetings(meetings, last_seen=last_seen, full_export=full_export)
        reporter.advance(detail=f"selected {len(selected)} meetings")
        reporter.finish_phase(f"selected {len(selected)} meetings")

        files_written = 0
        files_unchanged = 0
        files_skipped_empty_overwrite = 0
        errors: list[dict[str, str]] = []
        exported_records: list[dict[str, Any]] = []
        content_stats: dict[str, dict[str, int]] = {
            "notes": {"populated": 0, "empty": 0},
            "enhanced": {"populated": 0, "empty": 0},
            "transcript": {"populated": 0, "empty": 0},
        }
        warnings: list[dict[str, str]] = []

        reporter.start_phase("Export", len(selected))
        total_selected = len(selected)
        for index, m in enumerate(selected, start=1):
            meeting_id = str(m.get("id") or "").strip()
            if not meeting_id:
                reporter.advance(detail=f"{index}/{total_selected} skipped-invalid-id")
                continue

            title = str(m.get("title") or "Untitled")
            primary_dir = UNFILED_DIR_NAME  # resolved below from per-note data
            folder_name = meeting_folder_name(m.get("created_at"), title)
            md_dir = md_root / primary_dir / folder_name
            js_dir = json_root / primary_dir / folder_name

            try:
                extra_outputs: list[tuple[Path, str]] = []
                folder_membership: list[dict[str, Any]] = []

                if provider == "official":
                    note = client.get_note(meeting_id)
                    transcript = note.get("transcript") if isinstance(note.get("transcript"), list) else []
                    notes_raw = None
                    enhanced_raw = {
                        "summary": note.get("summary"),
                        "summary_text": note.get("summary_text"),
                        "summary_markdown": note.get("summary_markdown"),
                    }
                    notes_md = ""
                    enhanced_md = summary_to_markdown(
                        note.get("summary_markdown"),
                        note.get("summary_text") or note.get("summary"),
                    )
                    transcript_md = transcript_to_markdown(transcript)
                    folder_membership = [
                        item for item in (note.get("folder_membership") or []) if isinstance(item, dict)
                    ]
                    primary_dir = desired_primary_dir(folder_membership, {})
                    used = leaves_by_primary.setdefault(primary_dir, set())
                    leaf = meeting_folder_name(m.get("created_at"), title)
                    if leaf in used:
                        leaf = f"{leaf}--{meeting_id[:8]}"
                    used.add(leaf)
                    folder_name = leaf
                    md_dir = md_root / primary_dir / folder_name
                    js_dir = json_root / primary_dir / folder_name
                    meeting_snapshot = {
                        "id": meeting_id,
                        "object": note.get("object"),
                        "title": note.get("title"),
                        "created_at": note.get("created_at"),
                        "updated_at": note.get("updated_at"),
                        "web_url": note.get("web_url"),
                        "owner": note.get("owner"),
                        "calendar_event": note.get("calendar_event"),
                        "attendees": note.get("attendees"),
                        "folder_primary": primary_dir,
                        "folder_membership": folder_membership,
                    }
                    extra_outputs.append((js_dir / "note.json", json.dumps(note, ensure_ascii=False, indent=2) + "\n"))
                else:
                    metadata = client.get_metadata(meeting_id)
                    transcript = client.get_transcript(meeting_id)

                    notes_raw = metadata.get("notes")
                    enhanced_raw = enhanced_content_from(m, metadata)

                    notes_md = prosemirror_to_markdown(notes_raw if isinstance(notes_raw, dict) else None)
                    enhanced_md = enhanced_to_markdown(enhanced_raw)
                    transcript_md = transcript_to_markdown(transcript)
                    used = leaves_by_primary.setdefault(UNFILED_DIR_NAME, set())
                    if folder_name in used:
                        folder_name = f"{folder_name}--{meeting_id[:8]}"
                    used.add(folder_name)
                    md_dir = md_root / primary_dir / folder_name
                    js_dir = json_root / primary_dir / folder_name
                    meeting_snapshot = {
                        "id": meeting_id,
                        "title": title,
                        "created_at": m.get("created_at"),
                        "updated_at": m.get("updated_at"),
                        "workspace_id": m.get("workspace_id"),
                        "people": metadata.get("people"),
                        "creator": metadata.get("creator"),
                        "attendees": metadata.get("attendees"),
                        "folder_primary": primary_dir,
                        "folder_membership": [],
                        "provider": "internal",
                    }

                desired_paths[meeting_id] = (primary_dir, folder_name)

                meeting_warnings: list[str] = []
                if notes_md.strip():
                    content_stats["notes"]["populated"] += 1
                else:
                    content_stats["notes"]["empty"] += 1

                if enhanced_md.strip():
                    content_stats["enhanced"]["populated"] += 1
                else:
                    content_stats["enhanced"]["empty"] += 1
                    meeting_warnings.append("missing-enhanced-content")

                if transcript_md.strip():
                    content_stats["transcript"]["populated"] += 1
                else:
                    content_stats["transcript"]["empty"] += 1
                    meeting_warnings.append("missing-transcript-content")

                notes_fm = build_frontmatter(meeting_snapshot, "notes")
                enhanced_fm = build_frontmatter(meeting_snapshot, "enhanced")
                transcript_fm = build_frontmatter(meeting_snapshot, "transcript")

                outputs: list[tuple[Path, str]] = [
                    (md_dir / "notes.md", notes_fm + notes_md),
                    (md_dir / "enhanced.md", enhanced_fm + enhanced_md),
                    (md_dir / "transcript.md", transcript_fm + transcript_md),
                    (js_dir / "notes.json", json.dumps(notes_raw, ensure_ascii=False, indent=2) + "\n"),
                    (js_dir / "enhanced.json", json.dumps(enhanced_raw, ensure_ascii=False, indent=2) + "\n"),
                    (js_dir / "transcript.json", json.dumps(transcript, ensure_ascii=False, indent=2) + "\n"),
                    (js_dir / "meeting.json", json.dumps(meeting_snapshot, ensure_ascii=False, indent=2) + "\n"),
                ]
                outputs.extend(extra_outputs)

                file_info: list[dict[str, str]] = []
                for path, content in outputs:
                    changed, skipped = safe_write_export(
                        path,
                        content,
                        allow_empty_overwrite=allow_empty_overwrite,
                    )
                    if skipped:
                        files_skipped_empty_overwrite += 1
                        status = "skipped_empty_overwrite"
                    elif changed:
                        status = "written"
                    else:
                        status = "unchanged"

                    if changed:
                        files_written += 1
                    elif not skipped:
                        files_unchanged += 1

                    written_content = path.read_text(encoding="utf-8") if skipped and path.exists() else content
                    item = {
                        "path": str(path.relative_to(repo_root)),
                        "sha256": sha256_text(written_content),
                        "status": status,
                    }
                    file_info.append(item)

                for warning in meeting_warnings:
                    warnings.append({"id": meeting_id, "folder_name": folder_name, "warning": warning})

                exported_records.append(
                    {
                        "id": meeting_id,
                        "primary_dir": primary_dir,
                        "folder_name": folder_name,
                        "updated_at": m.get("updated_at"),
                        "folder_membership": folder_membership,
                        "files": file_info,
                        "warnings": meeting_warnings,
                    }
                )
                reporter.advance(detail=f"{index}/{total_selected} {folder_name}")
            except Exception as err:
                errors.append({"id": meeting_id, "error": str(err)})
                reporter.advance(detail=f"{index}/{total_selected} failed {folder_name}")

        reporter.finish_phase(f"ok={len(exported_records)} failed={len(errors)}")

        all_updated = [parse_iso((m or {}).get("updated_at")) for m in meetings]
        all_updated = [x for x in all_updated if x is not None]
        max_seen = (
            max(all_updated).isoformat().replace("+00:00", "Z") if all_updated else prev_state.get("last_max_updated_at_seen")
        )

        manifest = {
            "generated_at": utc_now_iso(),
            "totals": {
                "meetings_discovered": total_meetings,
                "meetings_selected": len(selected),
                "meetings_exported": len(exported_records),
                "files_written": files_written,
                "files_unchanged": files_unchanged,
                "files_skipped_empty_overwrite": files_skipped_empty_overwrite,
            },
            "content": content_stats,
            "run": {
                "provider": provider,
                "errors_count": len(errors),
                "full_export": full_export,
                "allow_empty_content_overwrite": allow_empty_overwrite,
            },
            "meetings": exported_records,
            "warnings": warnings,
            "errors": errors,
        }

        reporter.start_phase("Finalize", 1)
        reconcile_actions: list[dict[str, str]] = []
        if provider == "official":
            reconcile_actions = reconcile_export_layout(prev_paths, desired_paths, md_root, json_root)
        if reconcile_actions:
            manifest["run"]["reconcile_actions"] = reconcile_actions
        manifest_path = manifest_root / "manifest.json"
        safe_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if provider == "official":
            folders_path = manifest_root / "folders.json"
            safe_write(folders_path, json.dumps(build_folders_manifest(exported_records), ensure_ascii=False, indent=2) + "\n")

        if errors:
            reporter.advance(detail="completed with errors")
            reporter.finish_phase("completed with errors")
            print(f"Export completed with {len(errors)} errors", file=sys.stderr)
            return 1

        # Merge previously-known paths with paths resolved this run, so notes that weren't
        # re-fetched this run still have an accurate location on file for the next run's
        # reconciliation pass.
        merged_paths: dict[str, list[str]] = {mid: list(p) for mid, p in prev_paths.items()}
        for mid, p in desired_paths.items():
            merged_paths[mid] = list(p)
        new_state = {
            "last_successful_sync_utc": utc_now_iso(),
            "last_max_updated_at_seen": max_seen,
            "last_total_meetings": total_meetings,
            "paths_by_id": merged_paths,
        }
        safe_write(sync_state_path, json.dumps(new_state, ensure_ascii=False, indent=2) + "\n")
        reporter.advance(detail="manifest and sync state written")
        reporter.finish_phase("manifest and sync state written")

        print(
            json.dumps(
                {
                    "meetings_discovered": total_meetings,
                    "meetings_selected": len(selected),
                    "meetings_exported": len(exported_records),
                    "files_written": files_written,
                    "files_unchanged": files_unchanged,
                    "files_skipped_empty_overwrite": files_skipped_empty_overwrite,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        reporter.close()


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130)
