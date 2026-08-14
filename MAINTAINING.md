# 维护手册

本文面向仓库维护者，记录可重复的同步、验证、发布和清理流程。普通开发准备见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 数据边界

| 类型                   | 默认位置                                     | 是否提交   | 说明                         |
| ---------------------- | -------------------------------------------- | ---------- | ---------------------------- |
| 源码、测试、配置模板   | 仓库内                                       | 是         | Pull Request 的主要内容      |
| 站点固定页面           | `content/index.md`、`content/ATTRIBUTION.md` | 是         | 必须通过内容审计             |
| 已验证译文             | `content/**/*.md`                            | 是         | 必须含完整逐页溯源           |
| 上游 nLab 仓库         | `nlab-content/`                              | 否         | 可重新同步                   |
| 中央状态库及备份       | `state/`                                     | 否         | 运营数据，需单独备份         |
| 状态 JSON 导出         | `scripts/nlab_glossary.json`                 | 否         | 可由 `export` 重建           |
| 人工词典               | `scripts/dictionary.json`                    | 视内容而定 | 仅提交可公开且有权分发的条目 |
| 外部种子词典           | `scripts/external_glossary.json`             | 否         | 只在本地提供已授权数据       |
| 构建、日志、模型与依赖 | `public/`、`logs/`、`models/` 等             | 否         | 本地或可再生数据             |

## 日常同步

```bash
./manage.sh sync-repo
./manage.sh scan
./manage.sh status
```

`scan` 在修改状态前通过 SQLite backup API 写入
`state/nlab_state.db.bak`。它会拒绝异常的大规模上游删除；确认上游变化真实后，
才使用底层命令的 `--allow-large-prune` 参数。

建议把 `state/nlab_state.db`、其 `.bak` 文件和人工词典备份到仓库之外。
不要依赖 Git 保存这些运营数据。

## 翻译与复核

先使用小批次和 dry run 检查筛选范围：

```bash
./manage.sh translate --dry-run --limit 10
./manage.sh translate --limit 10
nlab-pipeline report
```

失败页面不会发布。连续失败达到阈值后进入 `needs_review`，应先检查源文、模型输出、
词典和占位符错误，再决定是否使用 `--force` 重试。人工修订词典后，分块缓存会因
词典哈希变化自动失效；也可用 `nlab-pipeline prune-cache --dry-run` 预览清理。

## 依赖更新

- pnpm 依赖使用 Dependabot 或 `pnpm update` 更新，并提交
  `package.json` 与 `pnpm-lock.yaml`。
- Python 依赖先修改 `pyproject.toml`，再按项目现有锁定流程更新
  `requirements.lock`。
- GitHub Actions 和 Docker 基础镜像由 Dependabot 检查。
- 不接受只修改清单而未同步锁文件的依赖 Pull Request。

依赖升级后至少运行完整质量检查；Quartz、构建工具或容器基础镜像升级还应执行实际
站点与 Docker 构建。

## 发布

### GitHub Pages

仓库变量：

- `NLAB_SITE_URL`：规范主机名和可选子路径
- `NLAB_OG_IMAGES`：大规模语料建议设为 `0`

部署工作流只上传 `public/` artifact，不把构建产物提交回源码分支。

### 手动 gh-pages

```bash
git remote add pages <repository-url>
# 在 .env 中设置 DEPLOY_REMOTE=pages
./manage.sh deploy
```

部署脚本不会默认使用 `origin`。普通推送需要确认；`--force` 仍使用远程快照约束的
`--force-with-lease`。推送前再次核对远程 URL。

### Docker

```bash
docker build --build-arg NLAB_SITE_URL=example.org -t nlab-chinese .
docker run --rm -p 8080:8080 nlab-chinese
```

运行镜像只包含 Nginx 与静态站点，不包含模型、状态库、日志或 nLab 上游仓库。

## 发布前清单

1. 工作区只包含计划提交的源码、文档、测试和锁文件变更。
2. `python3 scripts/check_docs.py` 与 `python3 scripts/check_content.py` 通过。
3. Python、TypeScript 和 Quartz 测试全部通过。
4. Quartz 站点构建成功；运行时变更还需 Docker 构建成功。
5. `git diff --check` 无空白错误，`git status --ignored` 中没有意外产物。
6. 归属、数据政策和许可证说明仍与提交内容一致。

## 本地清理

下列目录均可由测试或构建重新生成：

```text
public/
quartz/.quartz-cache/
.pytest_cache/
.ruff_cache/
**/__pycache__/
tsconfig.tsbuildinfo
```

清理时不要删除 `state/`、`logs/`、`nlab-content/`、人工词典或模型，除非已确认
不再需要并具备独立备份。

## 故障处理

- 状态库完整性失败：停止写入，从 `.bak` 或外部备份恢复，不要用空库覆盖。
- 翻译服务不可用：检查 `LLM_BACKEND`、`LLM_BASE_URL`、模型路径和超时设置。
- 发布校验失败：通过 `nlab-pipeline report` 查看阶段和错误，不要绕过门禁。
- 构建链接失败：先运行内容与文档审计，再核对 slug、别名和 `NLAB_SITE_URL`。

安全事件按 [SECURITY.md](SECURITY.md) 报告；内容归属或删除请求按
[DATA_POLICY.md](DATA_POLICY.md) 处理。
