"""Regression tests for the UI audit (landing overlap, auth, navigation/state, asset caching).

Static checks on the shipped sources; no browser needed. Rendered behaviour is covered by test_ui_rendered.py.
"""
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


HTML, CSS, APP, SCENE = _read("index.html"), _read("css", "styles.css"), _read("js", "app.js"), _read("js", "scene.js")


def test_single_supplied_background_only():
    assets = os.listdir(os.path.join(STATIC, "assets"))
    assert assets == ["bg.jpeg"], assets  # no competing hero/aurora images ship
    urls = set(re.findall(r"/static/assets/([\w.\-]+)", CSS + HTML + APP + SCENE))
    assert urls == {"bg.jpeg"}, urls


def test_no_canvas_effects_over_the_hero():
    # The canvas aurora (pillars + animated bar columns) rose into the hero CTAs; it must not come back.
    assert "<canvas" not in HTML
    assert "sky-canvas" not in CSS and "sky-canvas" not in HTML
    assert "createScene" not in SCENE and "getContext" not in SCENE


def test_hero_card_has_no_reflection_and_cannot_overflow_its_grid():
    assert "box-reflect" not in CSS  # mirrored the card's text under it as "ghost" copy
    assert re.search(r"\.console-body \{[^}]*grid-template-columns: minmax\(0, 1fr\)", CSS)


def test_no_duplicate_ids():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_one_canonical_section_per_topic():
    m = re.search(r"const SECTION_ROUTES = \{(.*?)\};", APP, re.S)
    mapping = dict(re.findall(r"'(/[\w\-]+)': '([\w\-]+)'", m.group(1)))
    assert set(mapping) == {"/product", "/how-it-works", "/evaluation", "/pricing", "/security", "/faq"}
    for route, section in mapping.items():
        assert f'id="{section}"' in HTML, (route, section)
        assert HTML.count(f'id="{section}"') == 1
    # The script-built duplicate pages for those topics are gone; only pages with no landing section remain.
    meta = re.search(r"const SITE_PAGE_META = \{(.*?)\n\};", APP, re.S).group(1)
    assert set(re.findall(r"'(/[\w\-]+)':", meta)) == {"/documentation", "/contact", "/privacy", "/terms"}
    assert "logo-icon-grid" not in APP  # the second logo implementation


def test_every_site_link_resolves():
    server = open(os.path.join(ROOT, "api.py"), encoding="utf-8").read()
    served = set(re.findall(r'"(/[\w\-]+)",', server.split("SITE_ROUTES = (")[1].split(")")[0]))
    for route in set(re.findall(r'data-site-route="([^"]+)"', HTML)) - {"/"}:
        assert route in served, route
    footer = HTML.split('<footer class="minimal-footer site-footer">')[1].split("</footer>")[0]
    assert 'href="#"' not in footer
    assert "id=" not in footer.replace('id="footerContactLink"', "")  # footer is cloned onto site pages; ids would duplicate


def test_auth_markup_is_minimal_and_not_autofilled_across_forms():
    auth = HTML.split('id="screen-auth"')[1].split('id="screen-workspace"')[0]
    for gone in ("auth-visual", "authSky", "auth-assurance", "authSubheading", "auth-hint"):
        assert gone not in auth, gone
    ac = dict(re.findall(r'id="(\w+)"[^>]*?autocomplete="([\w\-]+)"', auth))
    assert ac["loginEmail"] == "username" and ac["loginPassword"] == "current-password"
    # v1.0.6: only the sign-in form asks the browser to fill credentials; sign-up / reset e-mail fields opt out
    assert ac["registerEmail"] == "off" and ac["forgotEmail"] == "off"
    assert ac["registerPassword"] == ac["resetPassword"] == "new-password"
    # no value= attribute may pre-populate a credential field
    assert not re.search(r"<input[^>]*\bvalue=", auth)


def test_auth_state_is_cleared_and_not_copied_between_forms():
    assert "function resetAuthForms" in APP and "'pageshow'" in APP
    assert "$('loginEmail').value = email" not in APP
    assert APP.count("resetAuthForms();") >= 5


def test_password_policy_is_wired_to_the_markup():
    assert "function updatePasswordPolicy" in APP and "PASSWORD_RULES" in APP
    assert ".pw-policy li.is-met" in CSS


def test_stale_build_manifest_never_masks_edited_sources(tmp_path, monkeypatch):
    import api

    static = tmp_path / "static"
    (static / "dist").mkdir(parents=True)
    (static / "js").mkdir()
    (static / "css").mkdir()
    for rel in ("js/app.js", "js/scene.js", "js/ops.js", "css/styles.css", "css/ops.css"):
        (static / rel).write_text("/*src*/")
        stem, ext = os.path.splitext(os.path.basename(rel))
        (static / "dist" / f"{stem}.abc.min{ext}").write_text("/*old*/")
    manifest = {"/static/" + r: "/static/dist/" + os.path.splitext(os.path.basename(r))[0] + ".abc.min" + os.path.splitext(r)[1]
                for r in ("js/app.js", "js/scene.js", "js/ops.js", "css/styles.css", "css/ops.css")}
    mpath = static / "dist" / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    monkeypatch.setattr(api, "STATIC_DIR", str(static))
    monkeypatch.setattr(api, "IS_PRODUCTION", False)

    monkeypatch.setattr(api, "_ASSET_URLS", None)
    assert api._asset_urls()["/static/js/app.js"].startswith("/static/dist/")  # fresh build is used

    later = time.time() + 5
    os.utime(static / "js" / "app.js", (later, later))  # source edited after the build
    monkeypatch.setattr(api, "_ASSET_URLS", None)
    assert "?v=" in api._asset_urls()["/static/js/app.js"]  # stale bundle ignored


def test_evaluation_grid_tracks_and_cards_can_shrink_without_clipping():
    # Regression for the 390px evaluation-summary clipping: flexible tracks must
    # have a zero minimum and each card/content item must be allowed to shrink.
    grid = CSS.split(".evaluation-grid {", 1)[1].split("}", 1)[0]
    assert "repeat(4, minmax(0, 1fr))" in grid
    assert "overflow: hidden" not in grid, "do not mask overflowing evaluation cards"
    mobile = CSS.split("@media (max-width: 900px) {", 1)[1].split("\n}", 1)[0]
    assert "repeat(2, minmax(0, 1fr))" in mobile
    card = CSS.split(".evaluation-stat {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in card
    assert ".evaluation-stat > * { min-width: 0; overflow-wrap: anywhere; }" in CSS
