import asyncio
import os
import re
import threading
from typing import Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim


class TextOpsMixin:
    """
    JyProject 的文本与字幕 Mixin。
    """

    def add_text_simple(
        self,
        text: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "Subtitles",
        **kwargs,
    ):
        # 字幕模板 / 文字模板: 样式、花字、动画由模板决定, 其余样式参数忽略
        subtitle_template = kwargs.pop("subtitle_template", None)
        text_template = kwargs.pop("text_template", None)
        if subtitle_template or text_template:
            add = self.add_subtitle_template if subtitle_template else self.add_text_template
            return add(text, subtitle_template or text_template, start_time, duration, track_name,
                       clip_settings=kwargs.get("clip_settings"))

        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.text, track_name)

        start_us = safe_tim(start_time)
        dur_us = safe_tim(duration)

        # 兼容高层参数：动画参数不透传给 TextSegment，避免底层 __init__ 报 unexpected kw。
        anim_in = kwargs.pop("anim_in", None)
        anim_out = kwargs.pop("anim_out", None)
        anim_loop = kwargs.pop("anim_loop", None)
        anim_in_duration = kwargs.pop("anim_in_duration", None)
        anim_out_duration = kwargs.pop("anim_out_duration", None)
        anim_loop_duration = kwargs.pop("anim_loop_duration", None)
        flower = kwargs.pop("flower", None)
        bubble = kwargs.pop("bubble", None)

        # 仅透传 TextSegment 支持的字段
        allowed_keys = {"font", "style", "clip_settings", "border", "background", "shadow", "rich_spans"}
        text_kwargs = {k: v for k, v in kwargs.items() if k in allowed_keys}
        # 字体支持直接传名称 (内置枚举或剪映 11.x 面板字体)
        if isinstance(text_kwargs.get("font"), str):
            font_enum = self._resolve_enum(draft.FontType, text_kwargs["font"])
            if font_enum:
                text_kwargs["font"] = font_enum
            else:
                print(f"⚠️ Font not found: {text_kwargs['font']}")
                text_kwargs.pop("font")
        # 统一默认字幕样式：字号 5 + 黑色描边
        if "style" not in text_kwargs:
            text_kwargs["style"] = draft.TextStyle(size=5.0)
        if "border" not in text_kwargs:
            text_kwargs["border"] = draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0)
        if "clip_settings" not in text_kwargs:
            text_kwargs["clip_settings"] = draft.ClipSettings(transform_y=-0.8)

        seg = draft.TextSegment(text, draft.Timerange(start_us, dur_us), **text_kwargs)
        if flower:
            self.add_flower_effect(seg, flower)
        if bubble:
            self.add_text_bubble(seg, bubble)

        # 为动画名称做同义词+模糊解析，支持如 "Typewriter" -> "复古打字机"
        if anim_in:
            intro_enum = self._resolve_enum(draft.TextIntro, str(anim_in))
            if intro_enum:
                seg.add_animation(intro_enum, duration=anim_in_duration)
        if anim_out:
            outro_enum = self._resolve_enum(draft.TextOutro, str(anim_out))
            if outro_enum:
                seg.add_animation(outro_enum, duration=anim_out_duration)
        if anim_loop:
            loop_enum = self._resolve_enum(draft.TextLoopAnim, str(anim_loop))
            if loop_enum:
                seg.add_animation(loop_enum, duration=anim_loop_duration)

        self.script.add_segment(seg, track_name)
        return seg

    def add_flower_effect(self, segment, flower_name: str):
        """
        为文字片段添加花字 (剪映 11.x 面板花字, 名称或 resource_id)。
        若剪映本地已下载该花字 (Cache/artistEffect/<id>/<md5>/), 写入真实资源路径。
        """
        from utils.panel_assets import find_panel_asset, get_jianying_user_data_dirs

        row = find_panel_asset("panel_flower_texts.csv", str(flower_name))
        if not row:
            print(f"⚠️ Flower text not found: {flower_name} (先运行 sync_jy_panel.py 刷新花字索引)")
            return None
        if row.get("is_vip") == "1" and not getattr(self, "allow_vip", True):
            print(f"⛔ '{row['name']}' 为 VIP 花字，已按 allow_vip=False 跳过")
            return None
        segment.add_effect(row["resource_id"])
        for ud in get_jianying_user_data_dirs():
            local = os.path.join(ud, "Cache", "artistEffect", row["resource_id"], row.get("md5", ""))
            if row.get("md5") and os.path.isdir(local):
                segment.effect.path = local
                break
        else:
            print(f"ℹ️ 花字「{row['name']}」本地未下载，剪映打开草稿时会按 ID 获取")
        return segment

    def add_text_bubble(self, segment, bubble_name: str):
        """
        为文字片段添加气泡 (剪映 11.x 文字面板「气泡」, 名称或 resource_id, 见 panel_text_bubbles.csv)。
        若剪映本地已下载该气泡 (Cache/effect/<id>/<md5>/), 写入真实资源路径; 否则剪映打开草稿时按 ID 获取。
        """
        import glob

        from utils.panel_assets import _local_md5_index, find_panel_asset, get_jianying_user_data_dirs

        row = find_panel_asset("panel_text_bubbles.csv", str(bubble_name))
        if not row:
            print(f"⚠️ Text bubble not found: {bubble_name} (先运行 sync_jy_panel.py 刷新气泡索引)")
            return None
        if row.get("is_vip") == "1" and not getattr(self, "allow_vip", True):
            print(f"⛔ '{row['name']}' 为 VIP 气泡，已按 allow_vip=False 跳过")
            return None
        rid = row["resource_id"]
        segment.add_bubble(rid, rid)
        segment.bubble.name = row["name"]
        path = _local_md5_index().get(row.get("md5", ""), "")
        if not path:
            for ud in get_jianying_user_data_dirs():
                hits = glob.glob(os.path.join(glob.escape(ud), "Cache", "effect", rid, "*", ""))
                if hits:
                    path = hits[0].rstrip("/\\")
                    break
        if path:
            segment.bubble.path = path
        else:
            print(f"ℹ️ 气泡「{row['name']}」本地未下载，剪映打开草稿时会按 ID 获取")
        return segment

    def add_rich_text(
        self,
        text: str,
        highlights: list,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "Subtitles",
        **kwargs,
    ):
        """
        添加富文本字幕，支持按关键词高亮。
        highlights 格式: [{"word": "99%", "color": (1,0,0), "bold": True}, ...]
        """
        spans = []
        for h in highlights:
            word = h.get("word")
            if not word:
                continue

            start = 0
            while True:
                idx = text.find(word, start)
                if idx == -1:
                    break

                span = draft.RichTextSpan(
                    idx,
                    idx + len(word),
                    color=h.get("color"),
                    size=h.get("size"),
                    bold=h.get("bold"),
                    italic=h.get("italic"),
                    underline=h.get("underline"),
                )
                spans.append(span)
                start = idx + len(word)

        kwargs["rich_spans"] = spans
        return self.add_text_simple(text, start_time, duration, track_name, **kwargs)

    def set_subtitle_keywords(self, keywords_config: dict):
        """设置字幕关键词高亮配置，注入 draft_info.json 的 config 节点"""
        self.script.subtitle_keywords_config = keywords_config

    def add_narrated_subtitles(
        self,
        text: str,
        speaker: str = "zh_female_xiaopengyou",
        start_time: Union[str, int] = None,
        track_name: str = "Subtitles",
        **text_kwargs,
    ):
        """text_kwargs 透传给每条字幕的 add_text_simple, 如 flower="复古花字" / anim_in / font / subtitle_template。"""
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        curr_us = safe_tim(start_time)
        chosen_backend = None

        parts = [p for p in re.split(r"([，。！？、\n\r]+)", text) if p.strip()]
        sentences = []
        for i in range(0, len(parts), 2):
            s = parts[i]
            if i + 1 < len(parts):
                s += parts[i + 1]
            sentences.append(s.strip())

        for s in sentences:
            clean_text = s.rstrip("，。！？、\n\r ")
            if not clean_text:
                continue

            audio_seg, backend_used = self.add_tts_intelligent(
                clean_text,
                speaker=speaker,
                start_time=curr_us,
                tts_backend=chosen_backend,
                allow_fallback=(chosen_backend is None),
                return_backend=True,
            )
            if audio_seg:
                if chosen_backend is None:
                    chosen_backend = backend_used
                actual_dur_us = audio_seg.target_timerange.duration
                # 默认设置为屏幕底部 (transform_y=-0.8); 套用模板时沿用模板自己的位置与样式
                defaults = {}
                if not (text_kwargs.get("subtitle_template") or text_kwargs.get("text_template")):
                    defaults = {
                        "clip_settings": draft.ClipSettings(transform_y=-0.8),
                        "style": draft.TextStyle(size=5.0),
                        "border": draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0),
                    }
                self.add_text_simple(
                    clean_text,
                    start_time=curr_us,
                    duration=actual_dur_us,
                    track_name=track_name,
                    **{**defaults, **text_kwargs},
                )
                curr_us += actual_dur_us + 100000
            else:
                if chosen_backend is not None:
                    raise RuntimeError(
                        f"TTS segment failed under locked backend '{chosen_backend}'. "
                        "Stopped to avoid mixed voice providers."
                    )
        return curr_us

    def add_tts_intelligent(
        self,
        text: str,
        speaker: str = "zh_male_huoli",
        start_time: Union[str, int] = None,
        track_name: str = "VoiceOver",
        tts_backend: str = None,
        allow_fallback: bool = True,
        return_backend: bool = False,
    ):
        import hashlib
        import shutil

        from universal_tts import generate_voice_with_meta

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        temp_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(temp_dir, exist_ok=True)
        # 文件名按 (音色, 文本) 固定: 同一句配音每次生成都是同一个文件。剪映按草稿路径记住素材库列表,
        # 随机文件名会让重新生成后的草稿出现一串「媒体丢失」。合成结果另存全局缓存, 重跑不再联网
        key = hashlib.sha1(f"{speaker}|{tts_backend or ''}|{text}".encode("utf-8")).hexdigest()
        output_file = os.path.join(temp_dir, f"tts_{key[:12]}.ogg")
        cache_dir = os.environ.get("JY_TTS_CACHE") or os.path.expanduser("~/.cache/jianying-pro/tts")
        os.makedirs(cache_dir, exist_ok=True)
        cached = os.path.join(cache_dir, f"{key}.ogg")
        cached_backend_file = cached + ".backend"

        async def _generate():
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                shutil.copyfile(cached, output_file)
                used = open(cached_backend_file).read().strip() if os.path.exists(cached_backend_file) else (tts_backend or "sami")
                return output_file, used
            last = (None, None)
            for attempt in range(3):  # 剪映朗读 (SAMI) 偶尔握手超时, 整体重试 3 次
                last = await generate_voice_with_meta(
                    text,
                    output_file,
                    speaker,
                    backend=tts_backend,
                    allow_fallback=allow_fallback,
                    sami_retries=2,
                )
                if last and last[0]:
                    break
                await asyncio.sleep(2 * (attempt + 1))
            if last and last[0] and os.path.exists(last[0]):
                shutil.copyfile(last[0], cached)
                with open(cached_backend_file, "w") as f:
                    f.write(str(last[1] or ""))
            return last

        try:
            asyncio.get_running_loop()
            loop_running = True
        except Exception:
            loop_running = False

        if loop_running:
            box = {"path": None, "err": None}

            def _worker():
                try:
                    box["path"] = asyncio.run(_generate())
                except Exception as e:
                    box["err"] = e

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join()
            if box["err"] is not None:
                raise box["err"]
            actual_path, backend_used = box["path"] if box["path"] else (None, None)
        else:
            actual_path, backend_used = asyncio.run(_generate())

        if not actual_path:
            return (None, None) if return_backend else None
        seg = self.add_media_safe(actual_path, start_time, track_name=track_name)
        return (seg, backend_used) if return_backend else seg

    async def add_tts_intelligent_async(
        self,
        text: str,
        speaker: str = "zh_male_huoli",
        start_time: Union[str, int] = None,
        track_name: str = "VoiceOver",
    ):
        import uuid

        from universal_tts import generate_voice

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        temp_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(temp_dir, exist_ok=True)
        output_file = os.path.join(temp_dir, f"tts_{uuid.uuid4().hex[:8]}.ogg")
        actual_path = await generate_voice(text, output_file, speaker)
        if not actual_path:
            return None
        return self.add_media_safe(actual_path, start_time, track_name=track_name)
