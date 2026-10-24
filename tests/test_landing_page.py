from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
LANDING = HTML.split('<div id="screen-workspace"', 1)[0]
JS = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
CSS = (ROOT / 'static' / 'css' / 'styles.css').read_text(encoding='utf-8')


def test_landing_has_product_sections_and_real_ctas():
    for marker in (
        'Ask a question.',
        'Inspect the path.',
        'id="heroPrimaryCta"',
        'id="heroSecondaryCta"',
        'id="how-it-works"',
        'id="trace"',
        'id="features"',
        'id="architecture"',
        'id="evaluation"',
        'id="finalCta"',
    ):
        assert marker in HTML
    assert 'Try QuerySure' in HTML
    assert 'See how it works' in HTML


def test_landing_preserves_evaluation_and_trust_trace_hooks():
    for marker in (
        'id="evaluationSummary"',
        'id="evaluationMetrics"',
        'id="evaluationMethodModal"',
        'id="tab-trace"',
        'class="trust-trace-empty"',
        '/api/answer-trace',
        '/api/evaluation/product-summary',
    ):
        assert marker in HTML or marker in JS


def test_landing_ctas_are_wired_without_changing_workspace_navigation():
    assert "$('heroPrimaryCta').onclick = () => showAuthGate();" in JS
    assert "$('heroSecondaryCta').onclick = () => document.getElementById('how-it-works')?.scrollIntoView" in JS
    assert "$('finalCta').onclick = () => showAuthGate();" in JS
    assert "$('workspaceLogo').onclick" in JS


def test_landing_does_not_reference_missing_local_background_asset():
    # Every local asset the page references must ship with it. (bg.jpeg is the supplied workspace background:
    # this fails with a clear message until that file is copied to static/assets/.)
    for path in re.findall(r"(?:url|src)\(['\"]?([^'\")]+)", HTML + CSS):
        for prefix in ('/assets/', '/static/assets/'):
            if path.startswith(prefix):
                rel = path.split('?')[0].lstrip('/')
                rel = rel if rel.startswith('static/') else 'static/' + rel
                assert (ROOT / rel).exists(), f"{path} is referenced but {rel} is missing"
                break


def test_landing_uses_actual_stack_language_and_no_fake_social_proof():
    for marker in ('FastAPI', 'PostgreSQL', 'Redis', 'SQLite / Data Engine', 'S3-compatible', 'Groq · OpenAI · Anthropic', 'Deterministic + DeepEval'):
        assert marker in HTML
    for forbidden in ('customers', 'customer logos', 'trusted by', 'testimonials', '10,000+', '99.9% accurate', 'zero hallucinations'):
        assert forbidden.lower() not in LANDING.lower()


def test_registration_and_verification_states_are_wired_in_existing_auth_architecture():
    assert 'id="authVerificationState"' in HTML
    assert 'showVerificationState(' in JS
    assert "'Check your email'" in JS
    assert "'Email verified'" in JS
    assert "'Verification could not be completed'" in JS
    assert "await doRegister(email, password);" in JS
    assert "await doVerifyEmail(verifyToken);" in JS
