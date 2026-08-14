# 贡献指南

感谢你帮助改进 nLab-Chinese。小型修复可以直接提交 Pull Request；架构调整、
大规模内容变化或不兼容行为建议先开 Issue 说明目标和范围。

## 开发环境

```bash
corepack enable
pnpm install --frozen-lockfile
python3 -m pip install -e ".[dev]"
```

项目支持 Python 3.11 及以上、Node.js 22 及以上和 pnpm 11。个人配置放在
`.env`，不要在代码、文档或测试夹具中写入个人主目录、令牌、模型绝对路径或
私有服务地址。

## 变更原则

- 一个 Pull Request 聚焦一个问题，并包含与行为变化对应的测试或验证说明。
- 修改配置项时，同步更新 `.env.example`、README 和相关维护文档。
- 修改流水线状态、发布校验或数据库结构时，覆盖成功、失败和恢复路径。
- 不直接写入 `content/` 绕过 `publish_page` 的校验与原子发布流程。
- 翻译或发布变更必须保持公式、代码、链接目标、HTML 与标题层级。
- 修改 `quartz/` 中的上游代码时，保留原作者版权与 MIT 声明，并尽量缩小差异。
- 外部语料、术语表、模型产物或其他第三方数据只有在许可允许再分发时才能提交。

## 仓库边界

以下内容是本地或生成数据，不得提交：

- `.env`、API 密钥、GGUF 模型及其他凭据
- `state/`、`logs/`、`nlab-content/`、`public/`
- `node_modules/`、工具缓存、测试缓存与 TypeScript 增量缓存
- `scripts/nlab_glossary.json` 状态导出
- 未明确授权的 `scripts/external_glossary.json`

需要提交的内容包括源码、测试、配置模板、治理文档、站点固定页面，以及通过全部
发布门禁的中英对照页面。

## 提交前检查

```bash
python3 -m ruff check src tests scripts/cli.py scripts/check_content.py scripts/check_docs.py
python3 -m pytest
python3 scripts/check_content.py
python3 scripts/check_docs.py
pnpm check
pnpm test
NLAB_OG_IMAGES=0 pnpm quartz build
```

涉及容器运行时的改动还应执行：

```bash
docker build --tag nlab-chinese:test .
```

验证结束后不要提交 `public/` 或缓存目录。日常维护和清理步骤见
[MAINTAINING.md](MAINTAINING.md)。

## 文档规范

- 使用相对链接引用仓库文件，避免绑定某个 fork 或本地目录。
- 命令示例必须可以从仓库根目录执行。
- 区分默认值、示例值和部署必填值，避免记录会迅速过期的本机性能快照。
- 行为、参数或文件布局变化时，在同一 Pull Request 中更新相关文档。

## Pull Request

说明变更目的、用户可见影响、兼容性风险和实际验证结果。不要夹带无关格式化、
大型生成文件或本地状态变化。提交即表示你有权按仓库相应许可证提供该贡献。

所有参与者都应遵守 [社区行为准则](CODE_OF_CONDUCT.md)；安全问题请按
[SECURITY.md](SECURITY.md) 私密报告。
