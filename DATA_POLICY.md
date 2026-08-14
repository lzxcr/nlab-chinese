# 数据来源与处理政策（DATA POLICY）

## 上游数据

- 上游源：<https://github.com/ncatlab/nlab-content>（nLab 页面内容镜像）。
- 同步方式：`./manage.sh sync-repo` 浅克隆后增量拉取；`scan` 命令以
  页面源路径为主键重建中央状态库（SQLite，`state/nlab_state.db`）。
- 状态库不包含任何个人数据；仅记录标题、别名、分类、内容哈希、
  翻译状态与时间戳。

## 翻译过程

- 翻译由本地 llama.cpp 推理服务完成，内容不出本机。
- 每个分块均做结构保护（公式/代码/链接/HTML 不进模型）与质量校验；
  失败条目进入 `failed`/`needs_review` 状态，绝不静默发布。
- 分块翻译缓存（`chunk_cache`）以源文+模型+提示词+词典哈希为键，
  词典或提示词变化会自动失效。

## 发布内容

- 每页 frontmatter 记录来源与翻译元数据（见 ATTRIBUTION.md）。
- 站点 HTML 经 rehype-sanitize 白名单过滤，并附带 Content-Security-Policy。
- 搜索索引只携带中文译文，不重复携带英文原文快照。

## 删除请求

若某个页面的镜像不符合 nLab 使用条款或您的意愿，请提交 Issue 并注明页面；
我们会从状态库与输出目录中移除该页面（`skipped` 状态）。
