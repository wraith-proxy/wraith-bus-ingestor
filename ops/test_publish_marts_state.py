"""publish_marts_state's pure logic, and a lock against the 2026-09-24 bug:
a missing User-Agent header made every fetch 403 (Cloudflare/nginx treating
urllib's default UA as a bot) while curl to the identical URL passed -- the
timer "worked" (published a row every 5 minutes) for a while before anyone
noticed every single row said state=unreachable."""
import publish_marts_state as pms


def test_build_line_escapes_and_shapes_ilp():
    line = pms.build_line("ok", 101409, 1_700_000_000_000_000_000)
    assert line == "marts_state,state=ok n_models=101409i 1700000000000000000"


def test_esc_tag_strips_ilp_special_characters():
    assert pms.esc_tag("no,commas or=equals here") == "no_commas_or_equals_here"


def test_fetch_state_sends_a_real_user_agent(monkeypatch):
    """The actual regression: no header at all used to mean urllib's default
    'Python-urllib/x.y' UA, which got 403'd in production. Assert the header
    is present and not that default, not just that *some* value is sent."""
    seen = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"marts_state": "ok", "n_models": 5}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen['user_agent'] = req.get_header('User-agent')
        return FakeResponse()

    monkeypatch.setattr(pms.urllib.request, "urlopen", fake_urlopen)
    state, n_models = pms.fetch_state()

    assert state == "ok" and n_models == 5
    assert seen['user_agent'] and 'python-urllib' not in seen['user_agent'].lower()


def test_fetch_state_degrades_to_unreachable_on_any_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(pms.urllib.request, "urlopen", fake_urlopen)
    assert pms.fetch_state() == ("unreachable", 0)
