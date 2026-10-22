import argparse
import csv
import os
from typing import Dict, List

from utils.cli_protocol import emit_result, make_result
from utils.constants import SYNONYMS
from utils.errors import InfraError
from utils.logging_utils import setup_logger
from utils.panel_assets import find_tts_voice

logger = setup_logger("asset_search")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMPLATE_FILES = ("panel_subtitle_templates.csv", "panel_text_templates.csv")


def _ready_template_ids() -> set:
    """已采集预设 (可直接加进草稿) 的模板 resource_id。"""
    d = os.path.join(DATA_DIR, "template_presets")
    return {f[:-5] for f in os.listdir(d) if f.endswith(".json")} if os.path.isdir(d) else set()


def expand_query_with_synonyms(query: str) -> List[str]:
    terms = query.lower().split()
    expanded_terms = set(terms)
    for term in terms:
        if term in SYNONYMS:
            expanded_terms.update(SYNONYMS[term])
            continue
        for key, values in SYNONYMS.items():
            if term in key:
                expanded_terms.update(values)
    return list(expanded_terms)


def get_enum_key_from_ident(ident: str) -> str:
    ident_lower = ident.lower()
    for key, synonyms in SYNONYMS.items():
        if key in ident_lower:
            return key
        for syn in synonyms:
            if syn in ident_lower:
                return key
    return ""


def _iter_rows(filepath: str):
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        lines = [line for line in f if not line.startswith("#")]
    if not lines:
        return
    reader = csv.DictReader(lines)
    for row in reader:
        yield row


def row_is_vip(row: Dict[str, str]) -> bool:
    return row.get("is_vip") == "1" or (row.get("description") or "").upper() == "VIP"


def search_assets(
    query: str, category: str = None, limit: int = 20, vip_filter: str = "all",
    ready_only: bool = False,
) -> List[Dict[str, str]]:
    """vip_filter: all | free | vip; ready_only: 只返回已可直接使用的字幕/文字模板"""
    results: List[Dict[str, str]] = []
    ready_ids = _ready_template_ids()
    search_terms = expand_query_with_synonyms(query)

    if not os.path.exists(DATA_DIR):
        raise InfraError(f"Data directory not found: {DATA_DIR}")

    files_to_search: List[str]
    if category:
        normalized = category if category.endswith(".csv") else f"{category}.csv"
        if os.path.exists(os.path.join(DATA_DIR, normalized)):
            files_to_search = [normalized]
            # 同时搜索剪映 11.x 面板索引 (如 transitions.csv + panel_transitions.csv)
            if os.path.exists(os.path.join(DATA_DIR, f"panel_{normalized}")):
                files_to_search.append(f"panel_{normalized}")
        else:
            # 支持前缀匹配，如 -c filter => filters.csv
            all_csv = [f for f in os.listdir(DATA_DIR) if f.endswith(".csv")]
            files_to_search = [
                f for f in all_csv if f.startswith(category) or f.startswith(f"panel_{category}")
            ]
    else:
        files_to_search = [f for f in os.listdir(DATA_DIR) if f.endswith(".csv")]
    if ready_only:
        files_to_search = [f for f in files_to_search if f in TEMPLATE_FILES] or list(TEMPLATE_FILES)

    for filename in files_to_search:
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            logger.warning("Category file not found: %s", filename)
            continue

        for row in _iter_rows(filepath):
            if row.get("speaker_id") and "is_vip" not in row:
                voice = find_tts_voice(row["speaker_id"])
                row["is_vip"] = (voice or {}).get("is_vip", "0")
            if filename in TEMPLATE_FILES:
                row["ready"] = "1" if row.get("resource_id") in ready_ids else "0"
                if ready_only and row["ready"] != "1":
                    continue
            if vip_filter == "free" and row_is_vip(row):
                continue
            if vip_filter == "vip" and not row_is_vip(row):
                continue
            target_text = " ".join(
                [
                    row.get("identifier", ""),
                    row.get("description", ""),
                    row.get("category", ""),
                    row.get("title", ""),
                    row.get("name", ""),
                    row.get("categories", ""),
                    row.get("name_hint", ""),
                    row.get("speaker_id", ""),
                    row.get("voice_type", ""),
                ]
            ).lower()

            score = 0
            if query.lower() in target_text:
                score += 100
            for term in search_terms:
                if term in target_text:
                    score += 10

            if score > 0:
                row["score"] = score
                row["source_file"] = filename
                results.append(row)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[: max(1, limit)]


def format_results(results: List[Dict[str, str]]) -> str:
    if not results:
        return "❌ 未找到匹配项。尝试更简单的关键词或切换分类。"

    header = (
        f"{'Identifier':<25} | {'Category':<15} | {'API Key (Use This)':<20} | {'VIP':<4} | {'Source'}"
    )
    output = [header, "-" * len(header)]

    for r in results:
        ident = r.get("identifier") or r.get("title") or r.get("name") or r.get("name_hint") or "N/A"
        display_ident = ident if len(ident) <= 23 else ident[:20] + "..."
        cat = (r.get("sub_type") or r.get("category") or r.get("categories") or "N/A")[:15]
        src = r.get("source_file", "").replace(".csv", "")
        if r.get("ready") == "1":
            src += " ✅可直接用"
        elif r.get("ready") == "0":
            src += " ⏳需在剪映套用一次"
        if src.startswith("panel_"):
            # 面板素材: 直接用名称调用 (tts 用 voice_type)
            enum_key = r.get("voice_type") or r.get("name") or ident
        else:
            enum_key = (r.get("music_id") or r.get("speaker_id") or r.get("effect_id")
                        or get_enum_key_from_ident(ident) or ident)
        vip = "VIP" if row_is_vip(r) else ""
        output.append(f"{display_ident:<25} | {cat:<15} | {enum_key:<20} | {vip:<4} | {src}")

    return "\n".join(output)


def _list_categories() -> int:
    print("=== 剪映资产数据库概览 ===")
    if not os.path.exists(DATA_DIR):
        logger.error("Data directory not found: %s", DATA_DIR)
        return 1

    for filename in sorted(os.listdir(DATA_DIR)):
        if not filename.endswith(".csv"):
            continue
        filepath = os.path.join(DATA_DIR, filename)
        try:
            count = sum(1 for _ in _iter_rows(filepath))
        except Exception as e:
            logger.warning("Failed reading %s: %s", filename, e)
            count = 0
        print(f"{filename:<30} | {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="剪映资产搜索工具 (智能双语版)")
    parser.add_argument("query", nargs="?", default=None, help="搜索关键词")
    parser.add_argument("-c", "--category", help="限定分类（csv 文件名或前缀）")
    parser.add_argument("-l", "--limit", type=int, default=20, help="数量限制")
    parser.add_argument("--list", action="store_true", help="列出分类")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--ready", action="store_true", help="只看已可直接使用的字幕/文字模板")
    vip_group = parser.add_mutually_exclusive_group()
    vip_group.add_argument("--free-only", action="store_true", help="只看免费素材")
    vip_group.add_argument("--vip-only", action="store_true", help="只看 VIP 素材")
    args = parser.parse_args()
    vip_filter = "free" if args.free_only else ("vip" if args.vip_only else "all")

    if args.list:
        return _list_categories()

    if not args.query and not args.ready:
        parser.print_help()
        return 0

    if os.getenv("JY_AUTO_SYNC", "1") != "0":
        from utils.panel_assets import ensure_panel_fresh

        ensure_panel_fresh()
    logger.info("Searching '%s'...", args.query)
    try:
        search_results = search_assets(args.query or "", args.category, args.limit, vip_filter, args.ready)
        if args.json:
            emit_result(
                make_result(
                    True,
                    "ok",
                    "",
                    {
                        "query": args.query,
                        "category": args.category,
                        "count": len(search_results),
                        "results": search_results,
                    },
                ),
                True,
            )
        else:
            print(format_results(search_results))
        return 0
    except InfraError as e:
        logger.error(str(e))
        emit_result(make_result(False, "infra_error", str(e)), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
