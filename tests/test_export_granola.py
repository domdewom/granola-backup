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


def test_select_provider_prefers_official_api_key():
    assert m.select_provider("grn_test", '{"workos_tokens": "..."}') == "official"


def test_select_provider_falls_back_to_internal():
    assert m.select_provider("", '{"workos_tokens": "..."}') == "internal"
    assert m.select_provider("", "") is None


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


def test_enhanced_to_markdown_handles_prosemirror_doc():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Summary"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "From ProseMirror"}]},
        ],
    }
    md = m.enhanced_to_markdown(doc)
    assert "### Summary" in md
    assert "From ProseMirror" in md


def test_enhanced_to_markdown_handles_html_string():
    html = (
        "<h3>TLDR</h3>"
        "<ul><li><strong>Interviewer</strong>: Haley</li>"
        "<li>Review <a href=\"https://example.test\">link</a></li></ul>"
    )
    md = m.enhanced_to_markdown(html)
    assert "### TLDR" in md
    assert "- **Interviewer**: Haley" in md
    assert "- Review [link](https://example.test)" in md


def test_enhanced_to_markdown_preserves_plain_string():
    assert m.enhanced_to_markdown("Plain summary") == "Plain summary\n"


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


def test_official_transcript_to_markdown():
    items = [
        {
            "speaker": {"source": "speaker"},
            "start_time": "2026-01-01T10:00:05Z",
            "text": "Official transcript",
        }
    ]
    out = m.transcript_to_markdown(items)
    assert "[10:00:05][speaker] Official transcript" in out


def test_summary_to_markdown_prefers_markdown():
    out = m.summary_to_markdown("## Summary\n\n- Item", "Plain summary")
    assert out == "## Summary\n\n- Item\n"


def test_summary_to_markdown_falls_back_to_text():
    out = m.summary_to_markdown(None, "Plain summary")
    assert out == "Plain summary\n"


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


def test_official_client_sends_bearer_auth_and_lists_pages(monkeypatch):
    client = m.OfficialGranolaClient(
        api_base="https://example.test",
        api_key="grn_test",
        retry_cfg=m.RetryConfig(max_attempts=1),
        max_rps=1000,
    )
    calls = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        if len(calls) == 1:
            return Response({"notes": [{"id": "not_1", "updated_at": "2026-01-01T00:00:00Z"}], "hasMore": True, "cursor": "next"})
        return Response({"notes": [{"id": "not_2", "updated_at": "2026-01-02T00:00:00Z"}], "hasMore": False, "cursor": None})

    monkeypatch.setattr(m.requests, "get", fake_get)
    notes = client.list_meetings()

    assert [note["id"] for note in notes] == ["not_1", "not_2"]
    assert calls[0][1]["Authorization"] == "Bearer grn_test"
    assert calls[0][2] == {"page_size": m.PUBLIC_API_PAGE_SIZE}
    assert calls[1][2] == {"page_size": m.PUBLIC_API_PAGE_SIZE, "cursor": "next"}


def test_official_client_get_note_includes_transcript(monkeypatch):
    client = m.OfficialGranolaClient(
        api_base="https://example.test",
        api_key="grn_test",
        retry_cfg=m.RetryConfig(max_attempts=1),
        max_rps=1000,
    )
    calls = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "not_1", "transcript": []}

    def fake_get(url, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return Response()

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert client.get_note("not_1") == {"id": "not_1", "transcript": []}
    assert calls == [
        ("https://example.test/v1/notes/not_1", {"Authorization": "Bearer grn_test", "Accept": "application/json", "User-Agent": "granola-backup-exporter/1.0"}, {"include": "transcript"}, 45)
    ]


def test_build_folders_manifest():
    manifest = m.build_folders_manifest(
        [
            {
                "id": "not_1",
                "folder_name": "20260101_First",
                "folder_membership": [{"id": "fol_1", "object": "folder", "name": "Recruiting"}],
            },
            {
                "id": "not_2",
                "folder_name": "20260102_Second",
                "folder_membership": [{"id": "fol_1", "object": "folder", "name": "Recruiting"}],
            },
        ]
    )
    assert manifest["folders"][0]["id"] == "fol_1"
    assert manifest["folders"][0]["note_count"] == 2
    assert manifest["folders"][0]["notes"][1]["id"] == "not_2"


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
