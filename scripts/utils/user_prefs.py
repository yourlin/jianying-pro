"""
个人偏好覆盖层: 把用户自己的默认值叠加到已加载的规范 (场景 / 风格 / 画幅) 上。

仓库里的 references/{scenes,styles,formats}/*.json 是公开的通用规范; 个人偏好 (从手剪草稿学到的习惯)
放在仓库外的覆盖文件里, 编译时按组深合并上去:

    ~/.config/jianying-pro/style_overrides.json     (环境变量 JY_USER_PREFS 可改路径; 设成 0 / off 禁用)

覆盖文件格式 (所有以 _ 开头的键都是注释, 加载时忽略, 任何层级都一样):

    {
      "global":  {"scene": {...}, "style": {...}, "format": {...}},          # 所有片子
      "formats": {"vertical": {"style": {...}, "format": {...}}},             # 按画幅 id
      "styles":  {"tech": {"style": {...}}},                                  # 按风格 id
      "scenes":  {"talking_head": {"scene": {...}}}                           # 按场景 id
    }

生效顺序 global -> formats -> styles -> scenes, 后面的覆盖前面的 (越具体越优先)。
合并规则: dict 递归合并; 其它值 (包括 list 和 null) 直接替换。null 用来关掉某一项, 如 "grade": null。

用法 (在编译器加载完规范之后):

    from utils.user_prefs import apply_user_prefs
    scene, style, fmt, applied = apply_user_prefs(scene, style, fmt)
    for line in applied:
        print("   - 个人偏好:", line)
"""

import copy
import json
import os
from typing import Any, List, Mapping, Optional, Tuple

ENV_VAR = "JY_USER_PREFS"
DEFAULT_PATH = os.path.join("~", ".config", "jianying-pro", "style_overrides.json")
DISABLED_VALUES = {"0", "off", "false", "no", "none", "disable", "disabled"}

# 组名 -> (覆盖文件里的键, 用哪个规范的 id 选组)
GROUPS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("global", None),
    ("formats", "format"),
    ("styles", "style"),
    ("scenes", "scene"),
)
TARGETS = ("scene", "style", "format")


class UserPrefsError(ValueError):
    """覆盖文件格式不对 (不是合法 JSON / 结构不对)。"""


def prefs_path() -> Optional[str]:
    """覆盖文件路径; 被 JY_USER_PREFS=0 / off 禁用时返回 None。"""
    env = os.environ.get(ENV_VAR)
    if env is not None:
        if env.strip().lower() in DISABLED_VALUES or not env.strip():
            return None
        return os.path.expanduser(env.strip())
    return os.path.expanduser(DEFAULT_PATH)


def strip_comments(obj: Any) -> Any:
    """递归去掉以 _ 开头的键 (注释 / 证据说明)。"""
    if isinstance(obj, dict):
        return {
            k: strip_comments(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def load_user_prefs(path: Optional[str] = None) -> dict:
    """
    读覆盖文件 (已去掉 _ 注释)。文件不存在 / 被禁用时返回 {}。
    path 不传时按 JY_USER_PREFS / 默认路径找。
    """
    path = path if path is not None else prefs_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise UserPrefsError(f"个人偏好文件 {path} 不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise UserPrefsError(f"个人偏好文件 {path} 顶层必须是对象")
    data = strip_comments(data)
    for group, _sel in GROUPS:
        block = data.get(group)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise UserPrefsError(f"个人偏好文件 {path}: 「{group}」必须是对象")
        entries = [block] if group == "global" else list(block.values())
        for entry in entries:
            if not isinstance(entry, dict) or any(k not in TARGETS for k in entry):
                raise UserPrefsError(
                    f"个人偏好文件 {path}: 「{group}」下每组只能有 scene / style / format 三个键"
                )
    return data


def _fmt_value(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 80 else s[:77] + "..."


_MISSING = object()


def deep_merge(
    base: dict, overlay: Mapping, path: str = "", changes: Optional[list] = None
) -> dict:
    """
    把 overlay 深合并进 base (原地修改 base 并返回)。dict 递归; 其它值直接替换。
    changes 不为 None 时, 记下值真的变了的叶子: (路径, 旧值, 新值, 原来没有这个键)。
    """
    for k, v in overlay.items():
        p = f"{path}.{k}" if path else str(k)
        old = base.get(k, _MISSING)
        if isinstance(v, Mapping) and isinstance(old, dict):
            deep_merge(old, v, p, changes)
            continue
        new = copy.deepcopy(dict(v) if isinstance(v, Mapping) else v)
        if old is _MISSING or old != new:
            if changes is not None:
                changes.append((p, None if old is _MISSING else old, new, old is _MISSING))
            base[k] = new
    return base


def _profile_id(profile: Mapping, ids: Optional[Mapping], key: str) -> Optional[str]:
    if ids and ids.get(key):
        return str(ids[key])
    return profile.get("id") if isinstance(profile, Mapping) else None


def apply_user_prefs(
    scene: dict,
    style: dict,
    fmt: dict,
    ids: Optional[Mapping[str, str]] = None,
    prefs: Optional[dict] = None,
    path: Optional[str] = None,
) -> Tuple[dict, dict, dict, List[str]]:
    """
    把个人偏好叠加到已加载的规范上, 返回 (scene, style, fmt, applied)。

    - scene / style / fmt: load_profile 读出来的规范 (不会被修改, 返回的是合并后的副本)
    - ids: {"scene": "talking_head", "style": "tech", "format": "vertical"}; 不传时用各规范自己的 "id"
    - prefs: 直接给覆盖内容 (测试用); 不传时读 path / JY_USER_PREFS / 默认路径
    - applied: 生效的覆盖项, 每项一行, 如 "[formats.vertical] style.roles.title.anim_in: 渐显 -> 向上弹入";
      值没变的项不列出。没有覆盖文件或被禁用时, 原样返回 (同样是副本) 和空列表。
    """
    out = {
        "scene": copy.deepcopy(scene),
        "style": copy.deepcopy(style),
        "format": copy.deepcopy(fmt),
    }
    if prefs is None:
        prefs = load_user_prefs(path)
    else:
        prefs = strip_comments(prefs)
    applied: List[str] = []
    if not prefs:
        return out["scene"], out["style"], out["format"], applied
    sel = {k: _profile_id(out[k], ids, k) for k in TARGETS}
    for group, selector in GROUPS:
        block = prefs.get(group) or {}
        if selector is None:
            entry, label = block, group
        else:
            key = sel.get(selector)
            entry, label = (block.get(key) if key else None), f"{group}.{key}"
        if not entry:
            continue
        for target in TARGETS:
            overlay = entry.get(target)
            if not overlay:
                continue
            if out[target] is None:
                out[target] = {}
            changes: list = []
            deep_merge(out[target], overlay, target, changes)
            for p, old, new, added in changes:
                was = "(无)" if added else _fmt_value(old)
                applied.append(f"[{label}] {p}: {was} -> {_fmt_value(new)}")
    return out["scene"], out["style"], out["format"], applied
