import datetime as dt
import importlib.util
import sys
from pathlib import Path


def load_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_granola.py"
    spec = importlib.util.spec_from_file_location("export_granola", script)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


m = load_module()


def test_parse_supabase_workos_tokens():
    raw = '{"workos_tokens":"{\\"access_token\\":\\"a1\\",\\"refresh_token\\":\\"r1\\",\\"client_id\\":\\"c1\\"}"}'
    creds = m.parse_supabase_credentials(raw)
    assert creds["access_token"] == "a1"
    assert creds["refresh_token"] == "r1"
    assert creds["client_id"] == "c1"


def test_parse_supabase_cognito_tokens():
    raw = '{"cognito_tokens":"{\\"access_token\\":\\"a2\\",\\"refresh_token\\":\\"r2\\"}"}'
    creds = m.parse_supabase_credentials(raw)
    assert creds["access_token"] == "a2"
    assert creds["refresh_token"] == "r2"


def test_meeting_folder_name():
    folder = m.meeting_folder_name("2026-03-03T11:22:33Z", "Allie")
    assert folder == "20260303_Allie"


def test_meeting_folder_name_sanitizes_title():
    folder = m.meeting_folder_name("2026-03-03T11:22:33Z", "Allie / Team Sync!")
    assert folder == "20260303_Allie-Team-Sync"


def test_prosemirror_to_markdown():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
        ],
    }
    md = m.prosemirror_to_markdown(doc)
    assert "## Title" in md
    assert "Hello" in md


def test_transcript_to_markdown():
    items = [
        {
            "source": "microphone",
            "start_timestamp": "2026-01-01T10:00:05Z",
            "text": "First line",
        }
    ]
    out = m.transcript_to_markdown(items)
    assert "[10:00:05][microphone] First line" in out


def test_list_meetings_requests_last_viewed_panel(monkeypatch):
    client = m.GranolaClient(
        api_base="https://example.test",
        access_token="access",
        refresh_token="refresh",
        client_id="client",
        retry_cfg=m.RetryConfig(max_attempts=1),
        max_rps=1000,
    )
    bodies = []

    def fake_post(endpoint, body):
        bodies.append((endpoint, body))
        return {"docs": []}

    monkeypatch.setattr(client, "post", fake_post)
    assert client.list_meetings() == []
    assert bodies == [
        (
            "/v2/get-documents",
            {
                "limit": m.PAGE_SIZE,
                "offset": 0,
                "include_last_viewed_panel": True,
            },
        )
    ]


def test_enhanced_content_prefers_meeting_panel():
    meeting_doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "from list"}]}]}
    metadata_doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "from metadata"}]}]}
    meeting = {"last_viewed_panel": {"content": meeting_doc}}
    metadata = {"last_viewed_panel": {"content": metadata_doc}}
    assert m.enhanced_content_from(meeting, metadata) is meeting_doc


def test_enhanced_content_falls_back_to_metadata_panel():
    metadata_doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "from metadata"}]}]}
    assert m.enhanced_content_from({}, {"last_viewed_panel": {"content": metadata_doc}}) is metadata_doc


def test_safe_write_export_skips_empty_overwrite(tmp_path):
    path = tmp_path / "enhanced.md"
    path.write_text("Existing summary\n", encoding="utf-8")
    changed, skipped = m.safe_write_export(path, "", allow_empty_overwrite=False)
    assert changed is False
    assert skipped is True
    assert path.read_text(encoding="utf-8") == "Existing summary\n"


def test_safe_write_export_allows_empty_overwrite(tmp_path):
    path = tmp_path / "enhanced.md"
    path.write_text("Existing summary\n", encoding="utf-8")
    changed, skipped = m.safe_write_export(path, "", allow_empty_overwrite=True)
    assert changed is True
    assert skipped is False
    assert path.read_text(encoding="utf-8") == ""


def test_safe_write_export_does_not_protect_existing_null(tmp_path):
    path = tmp_path / "enhanced.json"
    path.write_text("null\n", encoding="utf-8")
    changed, skipped = m.safe_write_export(path, "[]\n", allow_empty_overwrite=False)
    assert changed is True
    assert skipped is False
    assert path.read_text(encoding="utf-8") == "[]\n"


def test_incremental_selector():
    meetings = [
        {"id": "1", "updated_at": "2026-01-01T00:00:00Z"},
        {"id": "2", "updated_at": "2026-01-03T00:00:00Z"},
    ]
    last_seen = dt.datetime.fromisoformat("2026-01-02T00:00:00+00:00")
    selected = m.select_incremental_meetings(meetings, last_seen=last_seen, full_export=False)
    assert [x["id"] for x in selected] == ["2"]


def test_progress_mode_off(monkeypatch):
    monkeypatch.setenv("PROGRESS_MODE", "off")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    reporter = m.create_progress_reporter()
    assert reporter.__class__.__name__ == "NullProgressReporter"


def test_progress_auto_ci_is_quiet(monkeypatch):
    monkeypatch.setenv("PROGRESS_MODE", "auto")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    reporter = m.create_progress_reporter()
    assert reporter.__class__.__name__ == "NullProgressReporter"
