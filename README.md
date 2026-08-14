# nLab-Chinese

将 [nLab](https://ncatlab.org/) 的数学、物理与哲学条目转换为经过结构校验的
中英对照静态站点。翻译流水线使用 Python，站点基于
[Quartz v4](https://quartz.jzhao.xyz/) 构建。

> [!IMPORTANT]
> 译文由机器生成，默认未经人工校对，仅供检索和学习参考。公式、定义和引文应以
> 每页附带的 nLab 英文原文及源页面为准。

## 工作方式

```text
ncatlab/nlab-content（上游源）
        │ sync-repo
        ▼
scan ──▶ state/nlab_state.db（SQLite 状态库，page_id 为源路径）
        │ slug 唯一化、别名消歧、删除保护
        ▼
translate（结构保护 → 分块 → LLM → 校验 → 分块缓存）
        │ 公式、代码、链接目标和 HTML 不发送给模型
        ▼
content/*.md（中英对照页面、YAML frontmatter、逐页溯源）
        │
        ▼
Quartz build ──▶ public/（静态站点、HTML 白名单、CSP、中文搜索）
        │
        ▼
GitHub Pages / Docker
```

核心设计：

- 源文件路径是稳定页面标识；slug 仅是带唯一约束的输出属性。
- 页面遵循
  `discovered → queued → translating → translated → validated → published`
  状态机；失败、截断或结构损坏的结果不会发布。
- 翻译前保护公式、代码、URL、wiki link 和 HTML；发布前再次比较公式、代码、
  链接目标、HTML 与标题层级。
- 分块缓存同时绑定源文、模型、提示词与词典版本，配置变化不会误用旧结果。
- 发布页记录源 URL、源修订、内容哈希、抓取时间、模型、提示词版本与质量状态。

## 环境要求

- Python 3.11 或更高版本
- Node.js 22 或更高版本
- pnpm 11（版本由 `package.json` 固定）
- 翻译时需要兼容 OpenAI API 的服务、Ollama，或
  [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server`

GPU 不是必需条件，但大规模本地翻译通常需要硬件加速。模型所需上下文、显存和
生成 token 上限差异很大，应以所选模型的说明为准。

## 快速开始

```bash
corepack enable
pnpm install --frozen-lockfile
python3 -m pip install -e ".[dev]"

cp .env.example .env
# 编辑 .env；本地 llama.cpp 至少需要设置 LLM_MODEL_PATH

./manage.sh sync-repo
./manage.sh scan
./manage.sh status
```

开始翻译时，在一个终端启动推理服务，再在另一个终端运行流水线：

```bash
./manage.sh serve
./manage.sh translate --limit 10
./manage.sh build
```

`manage.sh` 提供常用操作：

| 命令        | 用途                                                |
| ----------- | --------------------------------------------------- |
| `sync-repo` | 浅克隆或快速前进同步 nLab 上游仓库                  |
| `scan`      | 增量对账页面、别名和状态；大规模删除需要显式确认    |
| `audit`     | 交互式维护人工词典                                  |
| `tree`      | 输出分类树及待翻译列表                              |
| `status`    | 显示状态库摘要                                      |
| `translate` | 增量翻译；支持 `--limit`、`--category`、`--dry-run` |
| `serve`     | 前台启动本地 llama.cpp 服务                         |
| `build`     | 构建 Quartz 静态站点                                |
| `deploy`    | 将 `public/` 推送到显式配置的 `gh-pages` 远程       |

完整的状态导出、缓存清理、迁移、恢复和运行报告命令可通过
`nlab-pipeline --help` 查看。

## 配置

复制 [.env.example](.env.example) 为 `.env` 后按需覆盖默认值。`manage.sh`
与 Python CLI 都会加载仓库根目录的 `.env`。

| 变量                     | 默认值                       | 说明                                   |
| ------------------------ | ---------------------------- | -------------------------------------- |
| `LLM_BACKEND`            | `openai`                     | `openai`/兼容服务或 `ollama`           |
| `LLM_BASE_URL`           | `http://127.0.0.1:8080/v1`   | 推理服务地址                           |
| `LLM_MODEL`              | `gemma-4-12b-it`             | 发送给服务的模型标识                   |
| `LLM_MAX_TOKENS`         | `1024`                       | 单次请求最大生成 token                 |
| `LLM_NUM_CTX`            | `8192`                       | Ollama 请求的上下文大小                |
| `LLM_MODEL_PATH`         | `models/gemma-4-12b-it.gguf` | `manage.sh serve` 使用的 GGUF 文件     |
| `LLM_CTX`                | `4096`                       | `llama-server` 上下文窗口              |
| `MAX_WORKERS`            | `4`                          | 并发翻译请求数                         |
| `CHUNK_TARGET_TOKENS`    | `700`                        | 每个翻译分块的目标 token 预算          |
| `VALIDATE_MIN_CJK_RATIO` | `0.05`                       | 分块最低中文占比                       |
| `MAX_FAIL_COUNT`         | `3`                          | 转入 `needs_review` 前允许的连续失败数 |
| `DEPLOY_REMOTE`          | 未设置                       | `manage.sh deploy` 使用的 Git 远程名   |
| `NLAB_SITE_URL`          | `nlab-chinese.github.io`     | 规范主机名及可选子路径                 |
| `NLAB_REPOSITORY_URL`    | 未设置                       | 页脚中的项目源码链接                   |
| `NLAB_OG_IMAGES`         | `1`                          | 设为 `0` 关闭逐页 OG 图片              |

人工词典默认位于 `scripts/dictionary.json`。经过授权的外部种子词典可通过
`EXTERNAL_GLOSSARY_FILE` 在本地提供；仓库不分发许可证不明确的第三方词典。

## 目录结构

```text
content/                  Quartz 内容与流水线发布结果
nlab-content/             nLab 上游仓库（本地生成，不提交）
src/nlab_pipeline/        Python 流水线包
scripts/                  兼容入口、审计工具及本地词典
state/                    SQLite 状态库及备份（不提交）
tests/                    Python 测试
quartz/                   Quartz 站点生成器及项目定制
manage.sh                 常用操作封装
quartz.config.ts          站点与插件配置
quartz.layout.ts          页面布局
```

`public/`、状态库、日志、模型、上游仓库、依赖目录及 JSON 状态导出都是本地产物，
不应进入提交。详细边界和日常运维见 [MAINTAINING.md](MAINTAINING.md)。

## 质量检查

```bash
python3 -m ruff check src tests scripts/cli.py scripts/check_content.py scripts/check_docs.py
python3 -m pytest
python3 scripts/check_content.py
python3 scripts/check_docs.py
pnpm check
pnpm test
NLAB_OG_IMAGES=0 pnpm quartz build
```

CI 会执行相同的 Python、文档、内容、TypeScript、Quartz 构建与 Docker 构建检查。
测试和构建产生的 `public/`、`__pycache__/`、`.pytest_cache/`、
`.ruff_cache/`、`.quartz-cache/` 与 `tsconfig.tsbuildinfo` 不应提交。

## 部署

- GitHub Pages：部署工作流读取仓库变量 `NLAB_SITE_URL` 与
  `NLAB_OG_IMAGES`，构建后使用 GitHub Pages artifact 发布。
- Docker：

  ```bash
  docker build --build-arg NLAB_SITE_URL=example.org -t nlab-chinese .
  docker run --rm -p 8080:8080 nlab-chinese
  ```

- 手动部署：先添加有写权限的远程并在 `.env` 设置
  `DEPLOY_REMOTE=pages`。脚本不会隐式使用 `origin`；`--force` 使用
  `--force-with-lease`。

## 项目文档

- [CONTRIBUTING.md](CONTRIBUTING.md)：开发、内容和 Pull Request 规范
- [MAINTAINING.md](MAINTAINING.md)：状态备份、依赖更新、发布与清理手册
- [SECURITY.md](SECURITY.md)：安全支持范围与私密报告
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：社区行为准则
- [DATA_POLICY.md](DATA_POLICY.md)：数据流、保留和删除说明
- [ATTRIBUTION.md](ATTRIBUTION.md)：内容来源与逐页归属
- [NOTICE.md](NOTICE.md)：第三方内容与软件声明

## 许可与归属

本仓库程序代码采用 [MIT License](LICENSE.txt)。`quartz/` 保留 Quartz 作者的
MIT 版权与许可证副本。nLab 原文、机器译文和其他第三方内容不因此自动变为 MIT；
使用或分发前请阅读 [ATTRIBUTION.md](ATTRIBUTION.md)、
[DATA_POLICY.md](DATA_POLICY.md) 与 [NOTICE.md](NOTICE.md)。

nLab 当前鼓励在遵循学术惯例并注明来源的前提下使用和分发其内容，但未形成更正式的
统一许可证共识。公开运营镜像前，应持续核对 nLab 的最新说明和相关贡献者要求。
