"""
草稿明文副本 (Skill Copy)

剪映 11.x 打开并保存草稿后, draft_info.json / draft_meta_info.json 会被加密, skill 无法再读取。
skill 每次保存时额外写一份明文副本 (draft_info.skill.json / draft_meta_info.skill.json),
供继续编辑、克隆、导出字幕、检查等功能使用。

注意: 副本只包含 skill 最后一次保存的内容; 之后在剪映里做的手动修改不在副本中。

剪映自己也会在草稿目录写一份明文 template.json (根目录或 Timelines/<id>/), 在关闭草稿、回到首页时更新,
包含剪映里的手动修改; 剪映原生创建的草稿也有。只读场景 (查看/克隆/导出字幕) 取两者中较新的一份。
"""

import glob
import json
import os
import shutil
import time
from typing import Optional, Tuple

CONTENT_FILES = ("draft_info.json", "draft_content.json")
META_FILE = "draft_meta_info.json"
SKILL_CONTENT = "draft_info.skill.json"
SKILL_META = "draft_meta_info.skill.json"
# 剪映加密草稿的附属文件, 用明文副本恢复时需移走
CRYPTO_FILES = ("crypto_key_store.dat", "crypto_key_store.dat.bak", "draft_info.json.bak")


class EncryptedDraftError(Exception):
    """草稿已被剪映加密, 且没有可用的明文副本。"""


def is_plaintext_json(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(64).lstrip()
        return head.startswith(b"{")
    except OSError:
        return False


def content_path(draft_dir: str) -> Optional[str]:
    for name in CONTENT_FILES:
        p = os.path.join(draft_dir, name)
        if os.path.exists(p):
            return p
    return None


def is_encrypted(draft_dir: str) -> bool:
    p = content_path(draft_dir)
    return bool(p) and not is_plaintext_json(p)


def write_skill_copy(draft_dir: str) -> bool:
    """在 skill 保存后调用: 把明文草稿复制为 .skill.json 副本。"""
    src = content_path(draft_dir)
    if not src or not is_plaintext_json(src):
        return False
    shutil.copy2(src, os.path.join(draft_dir, SKILL_CONTENT))
    meta = os.path.join(draft_dir, META_FILE)
    if os.path.exists(meta) and is_plaintext_json(meta):
        shutil.copy2(meta, os.path.join(draft_dir, SKILL_META))
    return True


def app_plain_copy(draft_dir: str) -> Optional[str]:
    """剪映写的明文 template.json (多条时间线时取最新的), 没有返回 None。"""
    cands = [os.path.join(draft_dir, "template.json")]
    cands += glob.glob(os.path.join(draft_dir, "Timelines", "*", "template.json"))
    cands = [c for c in cands if os.path.exists(c) and is_plaintext_json(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def read_draft_content(draft_dir: str) -> Tuple[dict, str]:
    """
    读取草稿内容: 明文草稿直接读; 已加密则在 skill 明文副本和剪映明文 template.json 中取较新的一份。
    返回 (content, source), source 为 "draft" / "skill_copy" / "jianying_plain"。
    """
    p = content_path(draft_dir)
    if p and is_plaintext_json(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f), "draft"
    cands = []
    copy = os.path.join(draft_dir, SKILL_CONTENT)
    if os.path.exists(copy):
        cands.append((os.path.getmtime(copy), copy, "skill_copy"))
    plain = app_plain_copy(draft_dir)
    if plain:
        cands.append((os.path.getmtime(plain), plain, "jianying_plain"))
    for _mtime, path, source in sorted(cands, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except (OSError, ValueError):
            continue
        if content.get("tracks") is not None:
            return content, source
    raise EncryptedDraftError(
        f"草稿已被剪映加密，且没有可读的明文版本: {draft_dir}。"
        "skill 生成的草稿有明文副本; 剪映自己的草稿在剪映里关闭 (回到首页) 后会写出明文 template.json。"
    )


def read_draft_meta(draft_dir: str) -> Optional[dict]:
    for name in (META_FILE, SKILL_META):
        p = os.path.join(draft_dir, name)
        if os.path.exists(p) and is_plaintext_json(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


def restore_from_skill_copy(draft_dir: str) -> str:
    """
    用 skill 明文副本覆盖剪映加密后的草稿 (会丢弃之后在剪映里的手动修改)。
    覆盖前把剪映的版本备份到 draft_dir/.jy_app_backup_<时间戳>/, 返回备份目录。
    """
    copy = os.path.join(draft_dir, SKILL_CONTENT)
    if not os.path.exists(copy):
        raise EncryptedDraftError(f"没有 skill 明文副本: {draft_dir}")
    backup = os.path.join(draft_dir, f".jy_app_backup_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(backup, exist_ok=True)
    for name in CONTENT_FILES + (META_FILE,) + CRYPTO_FILES:
        p = os.path.join(draft_dir, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(backup, name))
    shutil.copy2(copy, os.path.join(draft_dir, "draft_info.json"))
    meta_copy = os.path.join(draft_dir, SKILL_META)
    if os.path.exists(meta_copy):
        shutil.copy2(meta_copy, os.path.join(draft_dir, META_FILE))
    return backup
