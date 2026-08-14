"""Import-safe console entry point with concise configuration failures."""

from __future__ import annotations

import sys
from importlib import import_module


def main() -> int:
    try:
        cli_main = import_module(".cli", __package__).main
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"❌ 配置加载失败: {exc}", file=sys.stderr)
        return 4
    return cli_main()
