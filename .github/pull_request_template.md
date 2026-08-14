## 变更

-

## 风险与兼容性

-

## 验证

- [ ] `python -m ruff check src tests scripts/cli.py scripts/check_content.py scripts/check_docs.py`
- [ ] `python -m pytest`
- [ ] `python scripts/check_content.py`
- [ ] `python scripts/check_docs.py`
- [ ] `pnpm check && pnpm test`
- [ ] 如影响站点：`NLAB_OG_IMAGES=0 pnpm quartz build`
