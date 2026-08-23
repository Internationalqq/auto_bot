"""Move exact duplicate EIS source/extracted files to recoverable trash."""

from __future__ import annotations

import argparse
import json

from autobot.source_file_versions import cleanup_existing_source_duplicates


def main() -> None:
    parser = argparse.ArgumentParser(description="Безопасная очистка точных дублей исходных файлов")
    parser.add_argument("--tender-id", default="", help="Номер одного тендера; без параметра — все тендеры")
    parser.add_argument("--downloads-only", action="store_true", help="Не очищать производный каталог extracted")
    parser.add_argument("--dry-run", action="store_true", help="Только показать план, ничего не перемещать")
    args = parser.parse_args()
    result = cleanup_existing_source_duplicates(
        args.tender_id.strip() or None,
        include_extracted=not args.downloads_only,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
