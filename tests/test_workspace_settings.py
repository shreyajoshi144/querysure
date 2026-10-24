from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_workspace_header_moves_account_and_session_details_into_settings():
    assert 'id="settingsToggleBtn"' in HTML
    assert 'id="settingsEmail"' in HTML
    assert 'id="settingsUserId"' in HTML
    assert 'id="settingsSessionId"' in HTML
    assert 'id="userBadge"' not in HTML
    assert 'id="sessionBadge"' not in HTML


def test_settings_reuses_existing_workspace_state():
    assert "currentUser = { id: '', email: '' };" in JS
    assert 'currentDataset = null;' in JS
    assert 'refreshSettingsView();' in JS
    assert "currentDataset = { name: res.db_name || 'Sample Database'" in JS
    # Uploads are async (202 + job polling): the dataset must come from the completed job result,
    # not the original 202 response.
    assert "currentDataset = { name: completed.db_name || 'Uploaded dataset'" in JS
    assert "apiGet('/api/usage')" in JS


def test_llm_key_is_never_rendered_and_settings_only_uses_masked_status():
    assert 'id="settingsLlmConfigured"' in HTML
    assert '••••••••' in HTML
    assert 'id="apiKeyInput"' in HTML
    assert "$('apiKeyInput').value = '';" in JS
    assert 'apiKeyOverride' in JS
    assert 'id="userBadge"' not in HTML


def test_existing_workspace_controls_remain_wired():
    assert "$('newSessionBtn').onclick = newSession;" in JS
    assert "$('disconnectBtn').onclick = () => location.reload();" in JS
    assert "$('signOutBtn').onclick" in JS
