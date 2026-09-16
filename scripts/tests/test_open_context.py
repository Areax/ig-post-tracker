"""open_context: shared context/page creation used both for the initial
browser context and every later proxy rotation, so cookies (storage_state)
carry forward across a rotation instead of resetting the session - see
PROXY_ROTATE_EVERY's comment in check_posts.py for why rotation exists at
all.
"""
import check_posts


class _FakeContext:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pages = []

    def new_page(self):
        page = object()
        self.pages.append(page)
        return page


class _FakeBrowser:
    def __init__(self, reject_storage_state=False):
        self.reject_storage_state = reject_storage_state
        self.contexts_created = []

    def new_context(self, **kwargs):
        if self.reject_storage_state and kwargs.get("storage_state") is not None:
            raise ValueError("stale storage_state shape")
        ctx = _FakeContext(**kwargs)
        self.contexts_created.append(ctx)
        return ctx


def test_passes_storage_state_and_proxy_through():
    browser = _FakeBrowser()
    state = {"cookies": ["whatever"]}
    proxy_cfg = {"server": "http://geo.iproyal.com:12321", "username": "u_session-abc", "password": "p"}

    context, page = check_posts.open_context(browser, state, proxy_cfg)

    assert context.kwargs["storage_state"] == state
    assert context.kwargs["proxy"] == proxy_cfg
    assert page in context.pages


def test_no_proxy_cfg_omits_proxy_kwarg():
    browser = _FakeBrowser()

    context, page = check_posts.open_context(browser, None, None)

    assert "proxy" not in context.kwargs
    assert context.kwargs["storage_state"] is None


def test_falls_back_to_stateless_context_on_bad_storage_state():
    browser = _FakeBrowser(reject_storage_state=True)
    state = {"cookies": "not actually valid shape"}

    context, page = check_posts.open_context(browser, state, None)

    assert len(browser.contexts_created) == 1
    assert "storage_state" not in context.kwargs
