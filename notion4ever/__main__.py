from notion4ever import notion2json
from notion4ever import structuring
from notion4ever import site_generation

import logging
import json
from pathlib import Path
import shutil
import argparse
import os

from notion_client import Client

# ---------------- logging page prefix ----------------

CURRENT_PAGE_PREFIX = "-"


class PagePrefixFilter(logging.Filter):
    def filter(self, record):
        record.page_prefix = CURRENT_PAGE_PREFIX
        return True


# ---------------- helpers ----------------

def get_page_title(notion: Client, page_id: str) -> str:
    try:
        page = notion.pages.retrieve(page_id=page_id)
        title_prop = page.get("properties", {}).get("title", {})
        title_items = title_prop.get("title", [])
        if title_items:
            return "".join(t.get("plain_text", "") for t in title_items)
    except Exception:
        pass
    return page_id

def normalize_page_ids(items):
    out = []
    for item in items or []:
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        for part in s.replace("\n", ",").replace("\r", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)

    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in {"true", "t", "yes", "y", "1"}:
        return True
    if value.lower() in {"false", "f", "no", "n", "0"}:
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got {value}")


# ---------------- main ----------------

def main():
    parser = argparse.ArgumentParser(
        description="Notion4ever: Export Notion pages to markdown/HTML static site"
    )

    parser.add_argument(
        "--notion_token", "-n",
        type=str,
        default=os.environ.get("NOTION_TOKEN"),
        help="Notion API token"
    )

    parser.add_argument(
        "--notion_page_id", "-p",
        action="append",
        default=[os.environ.get("NOTION_PAGE_ID")] if os.environ.get("NOTION_PAGE_ID") else [],
        help="Root page id. Можно несколько раз: -p id1 -p id2 или списком: -p 'id1,id2'"
    )

    parser.add_argument(
        "--output_dir", "-od",
        type=str,
        default="./_site",
        help="Output directory"
    )

    parser.add_argument("--templates_dir", "-td", type=str, default="./_templates")
    parser.add_argument("--sass_dir", "-sd", type=str, default="./_sass")

    parser.add_argument("--build_locally", "-bl", type=str_to_bool, default=False)
    parser.add_argument("--download_files", "-df", type=str_to_bool, default=True)

    parser.add_argument(
        "--site_url", "-su",
        type=str,
        default=os.environ.get("SITE_URL")
    )

    parser.add_argument("--include_footer", "-if", type=str_to_bool, default=False)
    parser.add_argument("--include_search", "-is", type=str_to_bool, default=False)

    parser.add_argument(
        "--logging_level", "-ll",
        type=str,
        default="INFO",
        choices=["INFO", "DEBUG"]
    )

    config = vars(parser.parse_args())

    CURRENT_PAGE_PREFIX = "-"

    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        # поле будет всегда, даже для httpx/urllib/чего угодно
        record.page_prefix = CURRENT_PAGE_PREFIX
        return record

    logging.setLogRecordFactory(record_factory)

    logging.basicConfig(
        format="[%(page_prefix)s] %(levelname)s: %(message)s",
        level=logging.DEBUG if config["logging_level"] == "DEBUG" else logging.INFO
    )

    logging.getLogger().addFilter(PagePrefixFilter())

    page_ids = normalize_page_ids(config["notion_page_id"])
    if not page_ids:
        raise RuntimeError("No notion page id provided. Use -p <page_id>")

    base_output_dir = Path(config["output_dir"]).resolve()

    # ✅ ALWAYS CLEAN BUILD: удаляем output_dir целиком перед запуском
    if base_output_dir.exists():
        shutil.rmtree(base_output_dir)
        logging.info("🧹 Clean build: removed output directory")

    notion = Client(auth=config["notion_token"])
    logging.info("🤖 Notion authentication completed")

    total_roots = len(page_ids)

    for idx, root_id in enumerate(page_ids, start=1):
        title = get_page_title(notion, root_id)
        CURRENT_PAGE_PREFIX = f"{title} {idx}/{total_roots}"

        root_output_dir = base_output_dir / f"root_{root_id}"
        root_output_dir.mkdir(parents=True, exist_ok=True)

        raw_file = root_output_dir / "notion_content.json"
        structured_file = root_output_dir / "notion_structured.json"

        root_config = dict(config)
        root_config["notion_page_id"] = root_id
        root_config["output_dir"] = str(root_output_dir)

        # -------- Stage 1: download raw (ALWAYS) --------
        raw_notion = {}

        logging.info("📡 Downloading raw notion content (no cache)")
        notion2json.notion_page_parser(
            root_id,
            notion=notion,
            filename=str(raw_file),
            notion_json=raw_notion
        )

        # -------- Stage 2: structuring --------
        logging.info("🤖 Structuring notion content")
        structured_notion = structuring.structurize_notion_content(
            raw_notion,
            root_config
        )

        with open(structured_file, "w", encoding="utf-8") as f:
            json.dump(structured_notion, f, ensure_ascii=False, indent=4)

        # -------- Stage 3: site generation --------
        if root_config["build_locally"]:
            structured_notion["base_url"] = str(root_output_dir.resolve())
        else:
            structured_notion["base_url"] = root_config["site_url"]

        logging.info(f"🌍 Generating site in {root_output_dir}")
        site_generation.generate_site(structured_notion, root_config)

        logging.info(f"✅ Finished root {root_id}")

    logging.info("🎉 All roots exported successfully")


if __name__ == "__main__":
    main()
