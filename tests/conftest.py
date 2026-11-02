import pytest


@pytest.fixture(autouse=True)
def _no_user_prefs(monkeypatch):
    """测试不受本机个人偏好 (~/.config/jianying-pro/style_overrides.json) 影响; 需要时在测试里覆盖。"""
    monkeypatch.setenv("JY_USER_PREFS", "off")
