"""Rendered-UI regression test: boots the real app and drives it in headless Chromium (skipped if Playwright/Chromium are absent)."""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = next((p for p in (os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome") if p and os.path.exists(p)), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chromium available")


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ, ENVIRONMENT="development", DATABASE_URL=f"sqlite:///{tmp}/ui.db", JWT_SECRET_KEY="ui-test-secret-0123456789abcdef0123",
               EMAIL_PROVIDER="dev", DEV_EMAIL_DIR=str(tmp / "mail"), PORT=str(port), APP_BASE_URL=f"http://127.0.0.1:{port}")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1); break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill(); pytest.fail("server did not start")
    yield url
    proc.terminate()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        yield b
        b.close()


VIEWPORTS = {"desktop": (1440, 900), "tablet": (820, 1180), "mobile": (390, 844)}


@pytest.mark.parametrize("name", VIEWPORTS)
def test_landing_and_auth_do_not_overflow_or_clip(browser, base, name):
    w, h = VIEWPORTS[name]
    page = browser.new_page(viewport={"width": w, "height": h})
    page.goto(base + "/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    assert page.evaluate("document.documentElement.scrollWidth") <= w
    # hero answer sheet: nothing wider than its card, and the question is not cut off
    assert page.evaluate("""() => { const c = document.querySelector('#heroConsole'); const r = c.getBoundingClientRect();
        return [...c.querySelectorAll('.console-q p, .specimen-note, .specimen-bars')].every(e => e.getBoundingClientRect().right <= r.right + 1); }""")
    assert page.evaluate("!document.querySelector('canvas')")
    page.click("#navStartBtn")
    page.wait_for_selector("#loginEmail", state="visible")
    assert page.evaluate("document.documentElement.scrollWidth") <= w
    assert page.evaluate("getComputedStyle(document.getElementById('googleAuthBtn')).display") == "none"  # Google unconfigured: no deployment notice
    page.close()


def test_header_and_footer_links_land_on_their_single_section(browser, base):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    targets = {"/product": "features", "/how-it-works": "how-it-works", "/evaluation": "evaluation", "/pricing": "pricing", "/security": "security", "/faq": "faq"}
    for route, section in targets.items():
        page.goto(base + "/", wait_until="networkidle")
        page.click(f'footer [data-site-route="{route}"]')
        page.wait_for_timeout(1500)
        assert page.url.endswith(route)
        assert page.evaluate("document.querySelector('.screen-state.active').id") == "screen-landing"
        assert abs(page.evaluate(f"document.getElementById('{section}').getBoundingClientRect().top")) < 10, route
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert abs(page.evaluate(f"document.getElementById('{section}').getBoundingClientRect().top")) < 10, f"refresh {route}"
    page.goto(base + "/privacy", wait_until="networkidle")
    assert page.evaluate("document.querySelectorAll('footer').length") == 2  # landing's (hidden) + the page's cloned one
    assert page.evaluate("[...document.querySelectorAll('[id]')].map(e=>e.id).length === new Set([...document.querySelectorAll('[id]')].map(e=>e.id)).size")
    page.close()


def test_typed_credentials_do_not_persist(browser, base):
    page = browser.new_page()
    page.goto(base + "/", wait_until="networkidle")
    page.click("#navSignInBtn")
    page.fill("#loginEmail", "someone@example.com")
    page.fill("#loginPassword", "Secret!Passw0rd1")
    page.click("#authBackBtn")
    page.click("#navStartBtn")
    assert page.input_value("#loginEmail") == "" and page.input_value("#loginPassword") == ""
    page.click('.auth-tab[data-form="register"]')
    page.fill("#registerPassword", "abc")
    assert page.get_attribute("#registerPolicy", "data-score") == "1"
    page.fill("#registerPassword", "Str0ng!Passw0rd#1")
    assert page.get_attribute("#registerPolicy", "data-score") == "5"
    page.close()


def test_all_evaluation_summary_cards_fit_at_390px(browser, base):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(base + "/", wait_until="networkidle")
    page.locator("#evaluationSummary").scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    result = page.evaluate("""() => {
      const grid = document.querySelector('#evaluationSummary');
      const bounds = grid.getBoundingClientRect();
      const cards = [...grid.querySelectorAll('.evaluation-stat')];
      return {
        viewport: innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        gridWidth: grid.clientWidth,
        gridScrollWidth: grid.scrollWidth,
        cardCount: cards.length,
        cards: cards.map(card => {
          const r = card.getBoundingClientRect();
          return {left: r.left, right: r.right, width: r.width,
            visible: r.width > 0 && r.left >= bounds.left - 1 && r.right <= bounds.right + 1};
        })
      };
    }""")
    assert result["cardCount"] == 4, result
    assert result["documentWidth"] <= 390, result
    assert result["gridScrollWidth"] <= result["gridWidth"] + 1, result
    assert all(card["visible"] for card in result["cards"]), result
    page.screenshot(path=os.path.join(os.environ.get("QUERYSURE_SCREENSHOT_DIR", "/tmp"), "evaluation_after_390.png"), full_page=True)
    page.close()
